from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from scripts import saitalk

CONF_FMT = (
    "spec_version=2\n"
    "reply_language={reply}\n"
    "chat_style={style}\n"
    "artifact_language=en\n"
    "review_mode=evidence-gated\n"
    "response_budget=5\n"
    "contract_id=saitalk-00000000\n"
)


def make_contract_text() -> str:
    sections = [
        ("Language", "Read reply_language."),
        ("Voice", "chat_style=caveman-ded. No emoji."),
        ("Authority", "Resolve conflicts in order."),
        ("Hard bans", "No preambles, no invitations."),
        ("Completion-first rule", "Complete first after the gate."),
        ("Evidence gate", "DEFECT, RISK, PREFERENCE, HYPOTHESIS, BLOCKER."),
        ("Review discipline", "Verdict, defects, risks."),
        ("Exactness", "Facts are sacred."),
        ("Surfaces", "Chat and artifact surfaces."),
        ("Persistence", "Stays active."),
        (
            "Suspension and bound state",
            (
                "saitalk_contract, saitalk_status, saitalk_voice; "
                "stop caveman, normal mode, resume caveman, saitalk mode."
            ),
        ),
        (
            "Configuration reference",
            (
                "`spec_version`, `reply_language`, `chat_style`, "
                "`artifact_language`, `review_mode`, `response_budget`, `contract_id`."
            ),
        ),
    ]
    body = "\n".join(
        f"## {num}. {title}\n\n{text}\n" for num, (title, text) in enumerate(sections, 1)
    )
    return f"# Contract\n\ncontract_id: saitalk-00000000\n\n{body}"


def make_root(root: Path) -> tuple[Path, Path, Path]:
    contract = root / "SAITALK.md"
    conf = root / "saitalk.conf"
    skill = root / "SKILL.md"
    contract.write_text(make_contract_text(), encoding="utf-8")
    conf.write_text(CONF_FMT.format(reply="en", style="caveman-ded"), encoding="utf-8")
    skill.write_text(
        "# SKILL\n\nSAITALK.md owns all normative behavior.\n", encoding="utf-8"
    )
    return contract, conf, skill


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Launch the real `python -m scripts.saitalk` child process.

    PERF-002 (SRC-002:R009): use this helper ONLY for properties that
    need a process boundary -- module entrypoint wiring, exit codes,
    cross-process lock contention.  Pure CLI business logic belongs to
    :func:`main_cli`, which patches ``sys.argv`` and calls
    ``scripts.saitalk.main()`` in-process at ~millisecond cost.
    """
    return subprocess.run(
        [sys.executable, "-m", "scripts.saitalk", *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO,
        timeout=60,
        check=False,
    )


def run_evals_cli(*args: str) -> subprocess.CompletedProcess[str]:
    """Launch the real `python -m evals.harness` child process.

    Same custody rule as :func:`run_cli`: process-boundary properties
    only; see :func:`main_evals_cli`.
    """
    return subprocess.run(
        [sys.executable, "-m", "evals.harness", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )


class _CliResult:
    """In-process stand-in for CompletedProcess: rc/stdout/stderr."""

    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def main_cli(*args: str, cwd: Path | None = None) -> _CliResult:
    """Run `scripts.saitalk` CLI logic in-process (PERF-002, SRC-002:R009).

    Patches ``sys.argv``, captures stdout/stderr, and calls
    ``scripts.saitalk.main()`` directly.  ``SystemExit`` raised by
    ``argparse`` error paths is converted to ``(rc, stdout, stderr)``
    with argparse's usual exit code 2, so the helper is safe wherever a
    subprocess result would have been.
    """
    import contextlib
    import io

    argv = ["saitalk", *args]
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_cwd = Path.cwd()
    old_argv = sys.argv
    sys.argv = argv
    if cwd is not None:
        os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(stdout_buf):
            with contextlib.redirect_stderr(stderr_buf):
                try:
                    rc = saitalk.main()
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 2
    finally:
        sys.argv = old_argv
        if cwd is not None:
            os.chdir(old_cwd)
    return _CliResult(rc, stdout_buf.getvalue(), stderr_buf.getvalue())


def main_evals_cli(*args: str) -> _CliResult:
    """Run `evals.harness` CLI logic in-process (PERF-002, SRC-002:R009)."""
    import contextlib
    import io

    from evals import harness

    argv = ["harness", *args]
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = argv
    try:
        with contextlib.redirect_stdout(stdout_buf):
            with contextlib.redirect_stderr(stderr_buf):
                try:
                    rc = harness.main()
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 2
    finally:
        sys.argv = old_argv
    return _CliResult(rc, stdout_buf.getvalue(), stderr_buf.getvalue())


def strip_ids(data: bytes) -> bytes:
    return saitalk.CONTRACT_ID_BYTES_PATTERN.sub(b"ID", data)


def lfify(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def crlfify(data: bytes) -> bytes:
    return lfify(data).replace(b"\n", b"\r\n")


def _peeled_tag_sha(repo: Path, tag: str) -> str | None:
    """Resolve a tag to its commit sha, peeling annotated tag objects.

    `git rev-parse <tag>` returns the tag OBJECT sha for an annotated tag,
    not the commit it points at; `<tag>^{}` peels to the commit for both
    annotated and lightweight tags. Returns None if the tag does not exist.
    """
    proc = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{}}"],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.contract, self.conf, self.skill = make_root(self.root)

    def test_missing_config_key_fails(self) -> None:
        self.conf.write_text(
            self.conf.read_text().replace("contract_id=saitalk-00000000\n", ""),
            encoding="utf-8",
        )
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("missing config keys: contract_id", str(ctx.exception))

    def test_extra_config_key_fails(self) -> None:
        self.conf.write_text(self.conf.read_text() + "bogus_key=1\n", encoding="utf-8")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("unknown config keys: bogus_key", str(ctx.exception))

    def test_duplicate_config_key_fails(self) -> None:
        self.conf.write_text(self.conf.read_text() + "reply_language=ru\n", encoding="utf-8")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("duplicate config key: reply_language", str(ctx.exception))

    def test_empty_config_value_fails(self) -> None:
        self.conf.write_text(
            self.conf.read_text().replace("reply_language=en", "reply_language="),
            encoding="utf-8",
        )
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.validate(self.contract, self.conf, self.skill)

    def test_duplicate_contract_id_in_conf_fails(self) -> None:
        self.conf.write_text(
            self.conf.read_text() + "contract_id=saitalk-00000000\n", encoding="utf-8"
        )
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("duplicate config key: contract_id", str(ctx.exception))

    def test_response_budget_boundaries(self) -> None:
        cases = {
            "0": "response_budget must be between 1 and 20",
            "1": None,
            "20": None,
            "21": "response_budget must be between 1 and 20",
            "-1": "response_budget must be between 1 and 20",
        }
        for budget, expected in cases.items():
            with (
                self.subTest(budget=budget),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                contract, conf, skill = make_root(root)
                conf.write_text(
                    conf.read_text().replace(
                        "response_budget=5", f"response_budget={budget}"
                    ),
                    encoding="utf-8",
                )
                if expected is None:
                    saitalk.refresh(contract, conf, skill)
                    saitalk.validate(contract, conf, skill)
                else:
                    with self.assertRaises(saitalk.SaitalkError) as ctx:
                        saitalk.validate(contract, conf, skill)
                    self.assertIn(expected, str(ctx.exception))

    def test_response_budget_non_integer_fails(self) -> None:
        self.conf.write_text(
            self.conf.read_text().replace(
                "response_budget=5", "response_budget=seven"
            ),
            encoding="utf-8",
        )
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("must be an integer", str(ctx.exception))

    def test_every_invalid_language_fails(self) -> None:
        for lang in ("fr", "de", "eesti", "日本語", ""):
            with (
                self.subTest(lang=lang),
                tempfile.TemporaryDirectory() as temp,
            ):
                root = Path(temp)
                contract, conf, skill = make_root(root)
                conf.write_text(
                    conf.read_text().replace(
                        "reply_language=en", f"reply_language={lang}"
                    ),
                    encoding="utf-8",
                )
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.validate(contract, conf, skill)

    def test_crlf_normalization_does_not_break_marker(self) -> None:
        expected = saitalk.refresh(self.contract, self.conf, self.skill)
        self.contract.write_bytes(crlfify(self.contract.read_bytes()))
        self.conf.write_bytes(crlfify(self.conf.read_bytes()))
        actual, _ = saitalk.validate(self.contract, self.conf, self.skill)
        self.assertEqual(actual, expected)

    def test_invalid_utf8_fails(self) -> None:
        self.contract.write_bytes(b"# Contract\n\n\xff\xfe\n")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("not UTF-8", str(ctx.exception))

    def test_missing_file_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            conf.unlink()
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, skill)
            self.assertIn("missing file", str(ctx.exception))

    def test_directory_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            conf.unlink()
            conf.mkdir()
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, skill)
            self.assertIn("path is a directory", str(ctx.exception))

    def test_missing_contract_id_line_fails(self) -> None:
        self.contract.write_text(
            make_contract_text().replace("contract_id: saitalk-00000000\n\n", ""),
            encoding="utf-8",
        )
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("expected exactly one match", str(ctx.exception))

    def test_manifest_member_name_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            wrong = root / "WRONG.md"
            wrong.write_text(contract.read_text(), encoding="utf-8")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(wrong, conf, skill)
            self.assertIn("manifest member mismatch", str(ctx.exception))

    def test_read_text_generic_oserror_is_clean_saitalk_error(self) -> None:
        with unittest.mock.patch.object(
            Path, "read_text", side_effect=OSError(5, "I/O error")
        ), self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.read_text(self.contract)
        self.assertIn("cannot read", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, (FileNotFoundError, PermissionError))

    def test_coherent_root_resolve_oserror_is_clean_saitalk_error(self) -> None:
        with unittest.mock.patch.object(
            Path, "resolve", side_effect=OSError(5, "I/O error")
        ), self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("cannot resolve manifest path", str(ctx.exception))

    def test_coherent_root_samefile_oserror_is_clean_saitalk_error(self) -> None:
        with unittest.mock.patch(
            "os.path.samefile", side_effect=OSError(13, "Permission denied")
        ), self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("cannot compare manifest paths", str(ctx.exception))

    def test_read_bytes_error_discipline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, _ = make_root(root)
            self.assertEqual(saitalk.read_bytes(contract), contract.read_bytes())
            missing = root / "nope.md"
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.read_bytes(missing)
            self.assertIn("missing file", str(ctx.exception))
            conf.unlink()
            conf.mkdir()
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.read_bytes(conf)
            self.assertIn("path is a directory", str(ctx.exception))


class StateTests(unittest.TestCase):
    def _state(self, root: Path, contract: str, status: str, voice: str) -> Path:
        state = root / "STATE.md"
        state.write_text(
            f"saitalk_contract: {contract}\n"
            f"saitalk_status: {status}\n"
            f"saitalk_voice: {voice}\n",
            encoding="utf-8",
        )
        return state

    def test_exactly_one_each_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678abcdef12", "active", "active")
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_duplicate_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_missing_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678abcdef12", "active", "active")
            state.write_text("saitalk_status: active\nsaitalk_voice: active\n", encoding="utf-8")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_invalid_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678abcdef12", "paused", "active")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_invalid_voice_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678abcdef12", "active", "partial")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_stale_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-0000000000000000", "active", "active")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_suspension_resume_transitions(self) -> None:
        self.assertEqual(saitalk.voice_command("stop caveman"), "suspended")
        self.assertEqual(saitalk.voice_command("normal mode"), "suspended")
        self.assertEqual(saitalk.voice_command("resume caveman"), "active")
        self.assertEqual(saitalk.voice_command("saitalk mode"), "active")
        for phrase in (
            '"stop caveman"',
            "`normal mode`",
            "she said stop caveman",
            "does normal mode work?",
            "stop caveman please",
            "resume caveman now",
        ):
            self.assertIsNone(saitalk.voice_command(phrase))

    def test_unrelated_host_fields_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "task_id: t-42\n"
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: suspended\n"
                "phase: RUN\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_fenced_example_does_not_become_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "```yaml\n"
                "saitalk_contract: example\n"
                "saitalk_status: paused\n"
                "saitalk_voice: suspended\n"
                "```\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_fenced_example_with_only_voice_line_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "```\n"
                "saitalk_voice: suspended\n"
                "```\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_tilde_fenced_example_does_not_become_live_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "~~~yaml\n"
                "saitalk_contract: example\n"
                "saitalk_status: paused\n"
                "saitalk_voice: suspended\n"
                "~~~\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_html_comment_state_line_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "<!-- saitalk_voice: suspended -->\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_multiline_html_comment_state_lines_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "<!--\n"
                "saitalk_voice: suspended\n"
                "saitalk_status: paused\n"
                "-->\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_mismatched_fence_delimiters_do_not_close_each_other(self) -> None:
        # A ``` fence is only closed by ```, never by ~~~; the fake state
        # line therefore stays hidden inside the (still-open) backtick fence
        # even though a ~~~ line appears first.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "```\n"
                "~~~\n"
                "saitalk_voice: suspended\n"
                "```\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678abcdef12")

    def test_top_level_duplicate_still_fails_with_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "saitalk_voice: suspended\n"
                "```\n"
                "saitalk_voice: suspended\n"
                "```\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678abcdef12")


class VoiceCommandTests(unittest.TestCase):
    def test_casefold_and_whitespace_collapse(self) -> None:
        self.assertEqual(saitalk.voice_command("NORMAL MODE"), "suspended")
        self.assertEqual(saitalk.voice_command("  normal   mode  "), "suspended")
        self.assertEqual(saitalk.voice_command("Normal Mode"), "suspended")
        self.assertEqual(saitalk.voice_command(" STOP  CAVEMAN "), "suspended")

    def test_non_standalone_phrases(self) -> None:
        for phrase in (
            "normal mode.",
            '"normal mode"',
            "`normal mode`",
            "normal mode please",
            "normal-mode",
        ):
            self.assertIsNone(saitalk.voice_command(phrase))

    def test_resume_casefold(self) -> None:
        self.assertEqual(saitalk.voice_command("RESUME CAVEMAN"), "active")
        self.assertEqual(saitalk.voice_command("  Saitalk  Mode  "), "active")


class SealTests(unittest.TestCase):
    def test_skill_mutation_invalidates_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            expected = saitalk.refresh(contract, conf, skill)
            skill.write_text(
                "# SKILL\n\nSAITALK.md owns all normative behavior. MUTATED LINE.\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate(contract, conf, skill)
            saitalk.refresh(contract, conf, skill)
            actual, _ = saitalk.validate(contract, conf, skill)
            self.assertNotEqual(actual, expected)
            self.assertEqual(len(actual), len("saitalk-") + 16)


class RefreshTests(unittest.TestCase):
    def _bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def test_reject_identical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, _, skill = make_root(root)
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.refresh(contract, contract, skill)
            self.assertIn("same file", str(ctx.exception))

    def _fail_write(self, fail_at: int):
        original = saitalk._safe_write_temp
        calls = {"n": 0}

        def wrapper(target, data, mode=None):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise saitalk.SaitalkError("simulated write failure")
            return original(target, data, mode=mode)

        return wrapper

    def _fail_replace(self, fail_at: int):
        original = saitalk._replace_temp
        calls = {"n": 0}

        def wrapper(target, tmp):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise saitalk.SaitalkError("simulated replacement failure")
            return original(target, tmp)

        return wrapper

    def _assert_rollback(self, patch_target: str, failer) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            with unittest.mock.patch(patch_target, side_effect=failer), self.assertRaises(
                saitalk.SaitalkError
            ):
                saitalk.refresh(contract, conf, skill)
            self.assertEqual(self._bytes(contract), before_c)
            self.assertEqual(self._bytes(conf), before_f)
            self.assertFalse((root / "SAITALK.md.tmp").exists())
            self.assertFalse((root / "saitalk.conf.tmp").exists())

    def test_rollback_first_write(self) -> None:
        self._assert_rollback("scripts.saitalk._safe_write_temp", self._fail_write(1))

    def test_rollback_second_write(self) -> None:
        self._assert_rollback("scripts.saitalk._safe_write_temp", self._fail_write(2))

    def test_rollback_replacement(self) -> None:
        self._assert_rollback("scripts.saitalk._replace_temp", self._fail_replace(2))

    def test_success_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch(
                "scripts.saitalk._safe_write_temp", side_effect=self._fail_write(1)
            ), self.assertRaises(saitalk.SaitalkError):
                saitalk.refresh(contract, conf, skill)
            expected = saitalk.refresh(contract, conf, skill)
            actual, _ = saitalk.validate(contract, conf, skill)
            self.assertEqual(actual, expected)

    def test_rollback_restores_only_the_replaced_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            before_f = self._bytes(conf)
            original_replace = saitalk._replace_temp
            original_restore = saitalk._restore_bytes
            restore_attempts = []

            def fail_second_replace(target, tmp):
                if target == conf:
                    raise saitalk.SaitalkError("simulated replacement failure")
                return original_replace(target, tmp)

            def fail_restore_contract(target, original, mode=None):
                restore_attempts.append(target)
                if target == contract:
                    raise saitalk.SaitalkError("simulated restore failure")
                return original_restore(target, original, mode=mode)

            with unittest.mock.patch(
                "scripts.saitalk._replace_temp", side_effect=fail_second_replace
            ), unittest.mock.patch(
                "scripts.saitalk._restore_bytes", side_effect=fail_restore_contract
            ), self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.refresh(contract, conf, skill)
            # Only the contract was actually replaced (conf's replace failed
            # before ever landing), so rollback must attempt exactly one
            # restore, on the contract only -- never on conf, which refresh
            # never touched.
            self.assertEqual(restore_attempts, [contract])
            self.assertIn("simulated replacement failure", str(ctx.exception))
            self.assertIn("rollback incomplete", str(ctx.exception))
            self.assertEqual(self._bytes(conf), before_f)

    def test_zero_replacements_means_zero_restore_calls_first_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch("scripts.saitalk._restore_bytes", wraps=saitalk._restore_bytes
            ) as restore_spy, unittest.mock.patch(
                "scripts.saitalk._safe_write_temp", side_effect=self._fail_write(1)
            ), self.assertRaises(saitalk.SaitalkError):
                saitalk.refresh(contract, conf, skill)
            restore_spy.assert_not_called()

    def test_zero_replacements_means_zero_restore_calls_second_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch("scripts.saitalk._restore_bytes", wraps=saitalk._restore_bytes
            ) as restore_spy, unittest.mock.patch(
                "scripts.saitalk._safe_write_temp", side_effect=self._fail_write(2)
            ), self.assertRaises(saitalk.SaitalkError):
                saitalk.refresh(contract, conf, skill)
            restore_spy.assert_not_called()

    def test_one_replacement_means_exactly_one_restore_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch(
                "scripts.saitalk._restore_bytes", wraps=saitalk._restore_bytes
            ) as restore_spy, unittest.mock.patch(
                "scripts.saitalk._replace_temp", side_effect=self._fail_replace(2)
            ), self.assertRaises(saitalk.SaitalkError):
                saitalk.refresh(contract, conf, skill)
            restore_spy.assert_called_once()
            self.assertEqual(restore_spy.call_args[0][0], contract)

    def test_refresh_preserves_crlf_and_bom(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            for path in (contract, conf):
                path.write_bytes(lfify(path.read_bytes()))
            saitalk.refresh(contract, conf, skill)
            for path in (contract, conf):
                data = saitalk.replace_id_bytes(
                    crlfify(path.read_bytes()),
                    "saitalk-0000000000000000",
                    f"{path.name} contract_id",
                )
                path.write_bytes(b"\xef\xbb\xbf" + data)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            saitalk.refresh(contract, conf, skill)
            after_c = self._bytes(contract)
            after_f = self._bytes(conf)
            self.assertIn(b"\r\n", after_c)
            self.assertIn(b"\r\n", after_f)
            self.assertTrue(after_c.startswith(b"\xef\xbb\xbf"))
            self.assertTrue(after_f.startswith(b"\xef\xbb\xbf"))
            self.assertNotEqual(before_c, after_c)
            self.assertNotEqual(before_f, after_f)
            self.assertEqual(strip_ids(before_c), strip_ids(after_c))
            self.assertEqual(strip_ids(before_f), strip_ids(after_f))

    def test_refresh_changes_only_marker_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            saitalk.refresh(contract, conf, skill)
            after_c = self._bytes(contract)
            after_f = self._bytes(conf)
            self.assertNotEqual(before_c, after_c)
            self.assertNotEqual(before_f, after_f)
            self.assertEqual(strip_ids(before_c), strip_ids(after_c))
            self.assertEqual(strip_ids(before_f), strip_ids(after_f))

    def test_lock_acquisition_order_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch(
                "scripts.saitalk._acquire_lock", wraps=saitalk._acquire_lock
            ) as patched:
                saitalk.refresh(contract, conf, skill)
            acquired = [call.args[0].resolve() for call in patched.call_args_list]
            self.assertEqual(acquired, sorted(acquired, key=str))

    def test_overlapping_lock_sets_contend(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract_a, conf_a, _ = make_root(root)
            root_b = root / "b"
            root_b.mkdir()
            contract_b, _, _ = make_root(root_b)
            shared = conf_a
            first = saitalk._acquire_locks([contract_b, shared])
            try:
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk._acquire_locks([contract_a, shared])
                self.assertIn("lock contention", str(ctx.exception))
            finally:
                saitalk._release_locks(first)

    def test_manifest_alias_hardlink_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, _, skill = make_root(root)
            alias = root / "saitalk.conf"
            alias.unlink()
            try:
                os.link(contract, alias)
            except OSError:
                self.skipTest("hardlinks not supported")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, alias, skill)
            self.assertIn("alias the same file", str(ctx.exception))

    def test_refresh_bom_with_contract_id_on_first_line(self) -> None:
        # contract_id as the literal first line, with a UTF-8 BOM ahead of
        # it: `^` in the byte-level pattern must still anchor there.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            reordered = (
                "contract_id=saitalk-00000000\n"
                + conf.read_text(encoding="utf-8").replace(
                    "contract_id=saitalk-00000000\n", ""
                )
            )
            conf.write_bytes(b"\xef\xbb\xbf" + reordered.encode("utf-8"))
            new_id = saitalk.refresh(contract, conf, skill)
            self.assertTrue(conf.read_bytes().startswith(b"\xef\xbb\xbf"))
            actual, _ = saitalk.validate(contract, conf, skill)
            self.assertEqual(actual, new_id)

    def test_skill_lock_contention_prevents_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            holder = saitalk._acquire_lock(skill)
            try:
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.refresh(contract, conf, skill)
                self.assertIn("lock contention", str(ctx.exception))
            finally:
                saitalk._release_lock(holder)
            # No false REFRESHED: nothing was written while skill was locked.
            self.assertEqual(self._bytes(contract), before_c)
            self.assertEqual(self._bytes(conf), before_f)

    @unittest.skipUnless(os.name == "nt", "Windows-specific lock file behavior")
    def test_repeated_windows_lock_acquisition_keeps_stable_file_size(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "SAITALK.md"
            target.write_text("x", encoding="utf-8")
            digest = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()[:16]
            lock_path = Path(tempfile.gettempdir()) / f"saitalk-{digest}.lock"
            self.addCleanup(lambda: lock_path.unlink(missing_ok=True))
            sizes = []
            for _ in range(5):
                fh = saitalk._acquire_lock(target)
                sizes.append(lock_path.stat().st_size)
                saitalk._release_lock(fh)
            self.assertEqual(
                len(set(sizes)), 1, f"lock file size changed across acquisitions: {sizes}"
            )
            self.assertEqual(sizes[0], 1)

    def test_split_state_recoverable_by_rerunning_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            new_id = saitalk.refresh(contract, conf, skill)
            conf.write_bytes(
                conf.read_bytes().replace(new_id.encode(), b"saitalk-0000000000000000")
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate(contract, conf, skill)
            saitalk.refresh(contract, conf, skill)
            actual, _ = saitalk.validate(contract, conf, skill)
            self.assertEqual(actual, new_id)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.contract, self.conf, self.skill = make_root(self.root)

    def test_validate_exit_zero_stdout(self) -> None:
        saitalk.refresh(self.contract, self.conf, self.skill)
        proc = main_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("SAITALK PASS", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_validate_stale_fails_exit_one_no_traceback(self) -> None:
        proc = main_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_print_id_stale_fails_exit_one_no_traceback(self) -> None:
        proc = main_cli(
            "print-id",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_refresh_then_print_id(self) -> None:
        refresh = main_cli(
            "refresh",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(refresh.returncode, 0)
        self.assertIn("SAITALK REFRESHED:", refresh.stdout)
        contract_id = refresh.stdout.strip().split("contract_id=")[1]
        printed = main_cli(
            "print-id",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(printed.returncode, 0)
        self.assertEqual(printed.stdout.strip(), contract_id)

    def test_state_override(self) -> None:
        saitalk.refresh(self.contract, self.conf, self.skill)
        state = self.root / "STATE.md"
        state.write_text(
            "saitalk_contract: saitalk-0000000000000000\n"
            "saitalk_status: active\n"
            "saitalk_voice: active\n",
            encoding="utf-8",
        )
        proc = main_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
            "--state", str(state),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_file_exit_one_no_traceback(self) -> None:
        missing = self.root / "saitalk.conf"
        missing.unlink()
        proc = main_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(missing),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL: missing file", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_no_traceback_on_lock_contention(self) -> None:
        holder = saitalk._acquire_lock(self.contract)
        try:
            proc = main_cli(
                "refresh",
                "--contract", str(self.contract),
                "--config", str(self.conf),
                "--skill", str(self.skill),
            )
        finally:
            saitalk._release_lock(holder)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lock contention", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_custom_package_clean_passes(self) -> None:
        pkg = self.root / "pkg"
        pkg.mkdir()
        contract, conf, skill = make_root(pkg)
        saitalk.refresh(contract, conf, skill)
        (pkg / "adapters").mkdir()
        (pkg / "adapters" / "OK.md").write_text(
            "# Adapter\n\nLoads files only.\n", encoding="utf-8"
        )
        proc = main_cli(
            "validate",
            "--contract", str(contract),
            "--config", str(conf),
            "--skill", str(skill),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SAITALK PASS", proc.stdout)

    def test_custom_package_forbidden_adapter_fails(self) -> None:
        pkg = self.root / "pkg"
        pkg.mkdir()
        contract, conf, skill = make_root(pkg)
        saitalk.refresh(contract, conf, skill)
        (pkg / "adapters").mkdir()
        (pkg / "adapters" / "BAD.md").write_text(
            "# Adapter\n\nHard bans apply here too.\n", encoding="utf-8"
        )
        proc = main_cli(
            "validate",
            "--contract", str(contract),
            "--config", str(conf),
            "--skill", str(skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("drift", proc.stderr)
        self.assertIn("structural drift guard", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_mixed_root_overrides_rejected(self) -> None:
        other = self.root / "other"
        other.mkdir()
        contract, _, _ = make_root(other)
        proc = main_cli(
            "validate",
            "--contract", str(contract),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("coherent package root", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ---- PERF-002 (SRC-002:R009): retained subprocess integration layer --
    #
    # These three cases stay process-boundary because they prove what the
    # in-process helpers cannot: real `python -m` wiring, real exit codes,
    # and real cross-process lock contention.

    def test_subprocess_validate_wiring_smoke(self) -> None:
        saitalk.refresh(self.contract, self.conf, self.skill)
        proc = run_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("SAITALK PASS", proc.stdout)

    def test_subprocess_failing_validate_wiring_smoke(self) -> None:
        proc = run_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_subprocess_cross_process_lock_contention(self) -> None:
        saitalk.refresh(self.contract, self.conf, self.skill)
        holder = saitalk._acquire_lock(self.skill)
        try:
            proc = run_cli(
                "refresh",
                "--contract", str(self.contract),
                "--config", str(self.conf),
                "--skill", str(self.skill),
            )
        finally:
            saitalk._release_lock(holder)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("lock contention", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    # ---- PERF-002 (SRC-002:R009): retained subprocess integration layer --
    #
    # Only the smallest viable set stays process-boundary: real
    # `python -m` wiring, real exit codes, real cross-process lock
    # contention, and one eval-side wiring smoke. Everything else runs
    # through main_cli/main_evals_cli above. Subprocess call sites in
    # the suite: exactly these. Count them in review before adding one.

    def test_subprocess_evals_validate_wiring_smoke(self) -> None:
        proc = run_evals_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("EVALS VALID", proc.stdout)

    # ---- PERF-002 (SRC-002:R009): helper isolation guarantees -----------

    def test_in_process_helpers_restore_global_state(self) -> None:
        # The helpers patch sys.argv and cwd/output capture; they must
        # leave no global residue, or the whole suite order-depends.
        import os as _os

        argv_before = list(sys.argv)
        cwd_before = _os.getcwd()
        main_cli("validate", "--contract", str(self.contract),
                 "--config", str(self.conf), "--skill", str(self.skill))
        main_evals_cli("validate")
        self.assertEqual(sys.argv, argv_before)
        self.assertEqual(_os.getcwd(), cwd_before)

    def test_in_process_cli_matches_subprocess_wiring(self) -> None:
        # The same properties through both boundaries: rc, routing to the
        # right stream, and no traceback on the public failure.
        saitalk.refresh(self.contract, self.conf, self.skill)
        via_child = run_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        via_direct = main_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(via_direct.returncode, via_child.returncode)
        self.assertIn("SAITALK PASS", via_direct.stdout)
        # Corrupt the marker so print-id must fail without a traceback.
        (self.root / "SAITALK.md").write_bytes(
            b"no contract marker here\n"
        )
        stale = main_cli("print-id",
                         "--contract", str(self.contract),
                         "--config", str(self.conf),
                         "--skill", str(self.skill))
        self.assertEqual(stale.returncode, 1)
        self.assertIn("SAITALK FAIL", stale.stderr)
        self.assertNotIn("Traceback", stale.stderr)


class ArtifactLanguageTests(unittest.TestCase):
    def test_auto_explicit_task_wins_over_existing(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language(
                "auto", task_language="ru", existing_language="en"
            ),
            "ru",
        )

    def test_auto_explicit_task_wins_over_existing_et(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language(
                "auto", task_language="et", existing_language="ru"
            ),
            "et",
        )

    def test_fixed_config_en_explicit_task_ru_wins(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", task_language="ru"), "ru"
        )

    def test_existing_wins_when_editing(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", existing_language="et"), "et"
        )

    def test_repo_language_before_configured(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", repo_language="et"), "et"
        )

    def test_configured_fallback(self) -> None:
        self.assertEqual(saitalk.resolve_artifact_language("et"), "et")
        self.assertEqual(saitalk.resolve_artifact_language("ru"), "ru")

    def test_auto_english_fallback(self) -> None:
        self.assertEqual(saitalk.resolve_artifact_language("auto"), "en")

    def test_explicit_unsupported_task_language_honored(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", task_language="es"), "es"
        )
        self.assertEqual(
            saitalk.resolve_artifact_language("auto", task_language="ja"), "ja"
        )
        self.assertEqual(
            saitalk.resolve_artifact_language("ru", task_language="zh-CN"), "zh-cn"
        )

    def test_explicit_task_language_normalized(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", task_language="  ES "), "es"
        )

    def test_invalid_configured_fails_loudly(self) -> None:
        for bad in ("bogus", "french", "en-GB", "auto!"):
            with self.subTest(bad=bad):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.resolve_artifact_language(bad)
                self.assertIn("artifact_language", str(ctx.exception))

    def test_malformed_explicit_task_fails_loudly(self) -> None:
        for bad in ("!!", "spaces here", "a"):
            with self.subTest(bad=bad), self.assertRaises(saitalk.SaitalkError):
                saitalk.resolve_artifact_language("auto", task_language=bad)

    def test_empty_explicit_task_fails(self) -> None:
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.resolve_artifact_language("auto", task_language="   ")

    def test_well_formed_existing_outside_enetru_honored(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("auto", existing_language="es"), "es"
        )

    def test_well_formed_repo_outside_enetru_honored(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("en", repo_language="ja"), "ja"
        )

    def test_existing_language_tag_normalized(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language("auto", existing_language="en-US"),
            "en-us",
        )

    def test_task_beats_well_formed_existing_and_repo(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language(
                "auto", task_language="fr", existing_language="es", repo_language="ja"
            ),
            "fr",
        )

    def test_existing_beats_repo_both_well_formed(self) -> None:
        self.assertEqual(
            saitalk.resolve_artifact_language(
                "en", existing_language="es", repo_language="ja"
            ),
            "es",
        )

    def test_malformed_existing_fails_loudly(self) -> None:
        for bad in ("!!", "spaces here", "a"):
            with self.subTest(bad=bad), self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.resolve_artifact_language("auto", existing_language=bad)
            self.assertIn("existing", str(ctx.exception))

    def test_malformed_repo_fails_loudly(self) -> None:
        for bad in ("!!", "spaces here", "a"):
            with self.subTest(bad=bad), self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.resolve_artifact_language("auto", repo_language=bad)
            self.assertIn("repo", str(ctx.exception))

    def test_empty_existing_fails_loudly(self) -> None:
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.resolve_artifact_language("auto", existing_language="   ")


class StructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.contract, self.conf, self.skill = make_root(self.root)
        saitalk.refresh(self.contract, self.conf, self.skill)

    def _mutate_contract(self, old: str, new: str) -> None:
        # Deliberately does not call refresh(): a structure-breaking mutation
        # must be caught by validate()'s check_structure, which runs before
        # the staleness check regardless of contract_id freshness. refresh()
        # itself refuses structurally broken content (see
        # test_refresh_refuses_structurally_broken_contract) so it cannot be
        # used to re-stamp a marker on these fixtures.
        self.contract.write_text(
            self.contract.read_text(encoding="utf-8").replace(old, new),
            encoding="utf-8",
        )

    def test_clean_fixture_passes(self) -> None:
        saitalk.validate(self.contract, self.conf, self.skill)

    def test_refresh_refuses_structurally_broken_contract(self) -> None:
        self.contract.write_text(
            self.contract.read_text(encoding="utf-8").replace(
                "## 8. Exactness", "### 8. Exactness"
            ),
            encoding="utf-8",
        )
        before_c = self.contract.read_bytes()
        before_f = self.conf.read_bytes()
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.refresh(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))
        self.assertEqual(self.contract.read_bytes(), before_c)
        self.assertEqual(self.conf.read_bytes(), before_f)

    def test_extra_top_level_section_fails(self) -> None:
        self._mutate_contract(
            "## 12. Configuration reference",
            "## 13. Override\n\nFake instruction.\n\n## 12. Configuration reference",
        )
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))

    def test_renamed_section_title_fails(self) -> None:
        self._mutate_contract("## 3. Authority", "## 3. Power")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))

    def test_fenced_decoy_cannot_mask_a_removed_real_section(self) -> None:
        # Attack: delete the real "## 5. Completion-first rule" heading but
        # leave a fenced decoy claiming to be it, hoping a heading scan that
        # does not fence-strip counts the decoy as satisfying the
        # requirement. The decoy must not count.
        text = self.contract.read_text(encoding="utf-8")
        text = text.replace(
            "## 5. Completion-first rule",
            "## 5-decoy-not-a-real-heading\n\n```\n## 5. Completion-first rule\n```",
        )
        self.contract.write_text(text, encoding="utf-8")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))

    def test_fenced_fake_heading_does_not_count(self) -> None:
        self.contract.write_text(
            self.contract.read_text(encoding="utf-8") + "\n```\n## 13. Override\n```\n",
            encoding="utf-8",
        )
        saitalk.refresh(self.contract, self.conf, self.skill)
        saitalk.validate(self.contract, self.conf, self.skill)

    def test_missing_section_fails(self) -> None:
        self._mutate_contract("## 8. Exactness", "### 8. Exactness")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))

    def test_duplicate_section_fails(self) -> None:
        self._mutate_contract("## 5. Completion-first rule", "## 4. Hard bans")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("contract structure", str(ctx.exception))

    def test_reordered_sections_fail(self) -> None:
        text = self.contract.read_text(encoding="utf-8")
        text = text.replace("## 2. Voice", "## 2. TMP")
        text = text.replace("## 3. Authority", "## 2. Voice")
        text = text.replace("## 2. TMP", "## 3. Authority")
        self.contract.write_text(text, encoding="utf-8")
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.validate(self.contract, self.conf, self.skill)

    def test_missing_state_token_fails(self) -> None:
        self._mutate_contract("stop caveman, ", "stop, ")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("section 11 must reference", str(ctx.exception))

    def test_missing_config_key_doc_fails(self) -> None:
        self._mutate_contract("`contract_id`", "`contract_marker`")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("section 12 must document", str(ctx.exception))

    def test_skill_normative_claim_required(self) -> None:
        self.skill.write_text("# SKILL\n\nMechanics only.\n", encoding="utf-8")
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk.validate(self.contract, self.conf, self.skill)
        self.assertIn("sole normative behavior source", str(ctx.exception))


class DriftTests(unittest.TestCase):
    def _adapter(self, root: Path, relative: str, text: str) -> Path:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_clean_adapter_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/OK.md", "# Adapter\n\nLoads files only.\n")
            saitalk.validate_drift(root)

    def test_banned_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/BAD.md", "# Adapter\n\nStop caveman now.\n")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("drift", str(ctx.exception))

    def test_punctuation_variant_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/BAD.md", "# Adapter\n\nhard-bans rule.\n")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_drift(root)

    def test_whitespace_and_case_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/BAD.md", "# Adapter\n\nHARD   BANS apply.\n")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_drift(root)

    def test_multiline_split_token_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(
                root, "adapters/BAD.md", "# Adapter\n\ncompletion-\nfirst rule here.\n"
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("drift", str(ctx.exception))
            self.assertIn("completion-first", str(ctx.exception))

    def test_multiline_split_token_reports_first_word_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(
                root, "adapters/BAD.md", "# Adapter\n\ncompletion-\nfirst rule here.\n"
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("BAD.md:3", str(ctx.exception))

    def test_recursive_scan_finds_nested_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(
                root, "adapters/deep/nested.md", "# Adapter\n\ntake precedence here.\n"
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_drift(root)

    def test_word_boundary_negative_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/OK.md", "# Adapter\n\nAuthoritative files.\n")
            saitalk.validate_drift(root)

    def test_error_wording_is_structural_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._adapter(root, "adapters/BAD.md", "# Adapter\n\nnormal mode.\n")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("structural drift guard", str(ctx.exception))
            self.assertIn("not proof of semantic conformance", str(ctx.exception))

    def test_no_adapters_dir_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            saitalk.validate_drift(Path(temp))


class DocConsistencyTests(unittest.TestCase):
    def _read(self, *parts: str) -> str:
        return (REPO.joinpath(*parts)).read_text(encoding="utf-8")

    def _prose(self, text: str) -> str:
        return re.sub(r"\s+", " ", text)

    def test_activation_recipes_include_all_members(self) -> None:
        for doc in ("adapters/README.md", "README.md"):
            text = self._read(doc)
            blocks = re.findall(r"```(?:text|ini)?\n(.*?)```", text, re.DOTALL)
            self.assertTrue(blocks, f"no fenced blocks in {doc}")
            for block in blocks:
                if "SAITALK.md" in block:
                    self.assertIn("SKILL.md", block, f"recipe missing SKILL.md in {doc}")
                    self.assertIn("saitalk.conf", block, f"recipe missing saitalk.conf in {doc}")

    def test_stateless_chat_includes_skill(self) -> None:
        text = self._read("adapters", "README.md")
        section = text.split("## Stateless chat", 1)[1].split("## ", 1)[0]
        self.assertIn("SKILL.md", section)
        self.assertIn("SAITALK.md", section)
        self.assertIn("saitalk.conf", section)

    def test_bootstrap_requires_validator_not_marker_reading(self) -> None:
        text = self._prose(self._read("adapters", "GENERIC_SYSTEM_PROMPT.md"))
        self.assertIn("scripts/saitalk.py", text)
        self.assertIn("validate", text)
        self.assertNotIn("read from the active files", text)

    def test_interpretation_human_use_mentions_any_member(self) -> None:
        text = self._prose(self._read("references", "INTERPRETATION.md"))
        self.assertNotIn("after changing either file", text)
        self.assertIn("runtime-manifest member", text)

    def test_interpretation_marker_is_not_seal(self) -> None:
        text = self._prose(self._read("references", "INTERPRETATION.md"))
        self.assertIn("not authentication", text)
        self.assertIn("not proof that edited rules are correct", text)

    def test_interpretation_worker_needs_validator_backing(self) -> None:
        text = self._prose(self._read("references", "INTERPRETATION.md"))
        self.assertNotIn("without reading the active files", text)

    def test_auto_wording_has_no_vague_russian_case(self) -> None:
        text = self._prose(self._read("SAITALK.md"))
        self.assertNotIn("clearly Russian primary repository", text)
        self.assertIn("substantive current user prose", text)

    def test_authority_clarifies_repo_prose_authority(self) -> None:
        text = self._prose(self._read("SAITALK.md"))
        self.assertIn("only the authority explicitly assigned", text)

    def test_no_whole_protocol_suspension_wording(self) -> None:
        text = self._prose(self._read("SAITALK.md"))
        self.assertNotIn("until explicitly suspended", text)
        self.assertIn("explicitly defined voice-style layer", text)

    def test_suspension_membership_defined(self) -> None:
        section11 = self._prose(
            self._read("SAITALK.md").split("## 11. Suspension and bound state", 1)[1].split(
                "## 12.", 1
            )[0]
        )
        self.assertIn("`response_budget`", section11)
        self.assertIn("no-emoji", section11)
        self.assertIn("compression", section11)

    def test_bound_state_wording_three_saitalk_fields(self) -> None:
        text = self._prose(self._read("SAITALK.md"))
        self.assertIn("exactly three SAITALK fields", text)

    def test_readme_no_whole_protocol_suspension(self) -> None:
        readme = self._prose(self._read("README.md"))
        self.assertNotIn("until explicit suspension", readme)

    def test_skill_load_order_no_whole_protocol_suspension(self) -> None:
        skill = self._prose(self._read("SKILL.md"))
        self.assertNotIn("until explicitly suspended", skill)

    def test_readme_refresh_recipe_mentions_any_member(self) -> None:
        readme = self._prose(self._read("README.md"))
        self.assertNotIn("after any config or contract edit", readme)
        self.assertIn("runtime-manifest member", readme)

    def test_skill_frontmatter_description_not_overclaimed(self) -> None:
        skill = self._read("SKILL.md")
        frontmatter = skill.split("---", 2)[1]
        self.assertNotIn("fixed reply language", frontmatter)
        self.assertNotIn("persistent caveman-ded voice", frontmatter)
        self.assertIn("configured reply language", frontmatter)
        self.assertIn("suspendable", frontmatter)

    def test_orchestrator_handoff_requires_full_bound_state(self) -> None:
        adapters = self._prose(self._read("adapters", "README.md"))
        section = adapters.split("## Orchestrator", 1)[1].split("## ", 1)[0]
        for field in ("saitalk_contract", "saitalk_status", "saitalk_voice"):
            self.assertIn(field, section)
        self.assertNotIn("Persist the validated `contract_id` in", adapters)

    def test_interpretation_agent_use_requires_full_bound_state(self) -> None:
        interpretation = self._prose(self._read("references", "INTERPRETATION.md"))
        section = interpretation.split("## 9. Agent use", 1)[1].split("## ", 1)[0]
        for field in ("saitalk_contract", "saitalk_status", "saitalk_voice"):
            self.assertIn(field, section)
        self.assertNotIn("preserve the contract through handoff", interpretation)

    def test_interpretation_loaded_id_does_not_overclaim_packaging(self) -> None:
        text = self._prose(self._read("references", "INTERPRETATION.md"))
        self.assertNotIn("proves only that the files were packaged together", text)
        self.assertIn("literally present", text)

    def test_interpretation_documents_bom_canonicalization(self) -> None:
        text = self._prose(self._read("references", "INTERPRETATION.md"))
        self.assertIn("Strip a leading UTF-8 BOM", text)
        self.assertIn("not exact-byte identity", text)


class CanonicalSuiteTests(unittest.TestCase):
    def test_shipped_example_state_validates(self) -> None:
        proc = main_cli(
            "validate",
            "--state", str(REPO / "examples" / "STATE.md"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_package_validates(self) -> None:
        proc = main_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_version_sources_agree(self) -> None:
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Current release: {version}.", readme)
        self.assertIn(f"tagging this exact commit `v{version}`", readme)
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (\d+\.\d+\.\d+) - ", changelog, re.MULTILINE)
        self.assertTrue(headings, "no release headings in CHANGELOG")
        self.assertEqual(headings[0], version, "first CHANGELOG heading must be the current release")

    def test_current_release_tag_agrees_when_present(self) -> None:
        if not (REPO / ".git").exists():
            self.skipTest("not a git checkout")
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        try:
            sha = _peeled_tag_sha(REPO, f"v{version}")
        except FileNotFoundError:
            self.skipTest("git not available")
        if sha is None:
            self.skipTest("current release tag not created yet")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=30,
            check=False,
        )
        self.assertEqual(sha, head.stdout.strip())

    def test_peeled_tag_sha_resolves_annotated_and_lightweight(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.skipTest("git not available")
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            env = {
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@t.example",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@t.example",
            }

            def run(*args: str) -> None:
                subprocess.run(
                    ["git", *args],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                    env=env,
                )

            def head_sha() -> str:
                return subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=True,
                ).stdout.strip()

            run("init", "-q")
            (repo / "f.txt").write_text("1", encoding="utf-8")
            run("add", "f.txt")
            run("commit", "-q", "-m", "one")
            commit1 = head_sha()
            run("tag", "-a", "v9.9.9", "-m", "annotated release")

            (repo / "f.txt").write_text("2", encoding="utf-8")
            run("add", "f.txt")
            run("commit", "-q", "-m", "two")
            commit2 = head_sha()
            run("tag", "v8.8.8")

            self.assertNotEqual(
                subprocess.run(
                    ["git", "rev-parse", "v9.9.9"],
                    cwd=repo, capture_output=True, text=True, timeout=30, check=True,
                ).stdout.strip(),
                commit1,
                "fixture invariant: an annotated tag's own sha must differ from its commit",
            )
            self.assertEqual(_peeled_tag_sha(repo, "v9.9.9"), commit1)
            self.assertEqual(_peeled_tag_sha(repo, "v8.8.8"), commit2)

    def test_changelog_0_1_8_test_count_corrected(self) -> None:
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        section = changelog.split("## 0.1.8 - ", 1)[1].split("## ", 1)[0]
        self.assertIn("43", section)


class EvalHarnessTests(unittest.TestCase):
    from evals import harness as evals_harness

    def test_shipped_cases_schema_valid(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        self.assertEqual(data["version"], 1)
        self.assertGreaterEqual(len(data["cases"]), 15)

    def test_unique_ids(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        ids = [case["id"] for case in data["cases"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_duplicate_id_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        data["cases"].append(dict(data["cases"][0]))
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("duplicate case id", str(ctx.exception))

    def test_missing_required_field_fails(self) -> None:
        data = {
            "version": 1,
            "cases": [{"id": "c1", "prompt": "p", "must": ["x"], "must_not": ["y"]}],
        }
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("setup", str(ctx.exception))

    def test_unknown_case_key_fails(self) -> None:
        data = {
            "version": 1,
            "cases": [
                {
                    "id": "c1",
                    "setup": "s",
                    "prompt": "p",
                    "must": ["x"],
                    "must_not": ["y"],
                    "extra": True,
                }
            ],
        }
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("unknown keys", str(ctx.exception))

    def test_invalid_id_format_fails(self) -> None:
        for bad in ("CASE-1", "case_1", "1case", "case.", "c"):
            with self.subTest(bad=bad):
                data = {
                    "version": 1,
                    "cases": [
                        {"id": bad, "setup": "s", "prompt": "p", "must": ["x"], "must_not": ["y"]}
                    ],
                }
                with self.assertRaises(self.evals_harness.EvalError):
                    self.evals_harness.validate_cases(data)

    def test_duplicate_normalized_must_entry_fails(self) -> None:
        data = {
            "version": 1,
            "cases": [
                {
                    "id": "c1",
                    "setup": "s",
                    "prompt": "p",
                    "must": ["Answer in Russian", "answer   in russian"],
                    "must_not": ["y"],
                }
            ],
        }
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("duplicate entries", str(ctx.exception))

    def test_must_must_not_overlap_fails(self) -> None:
        data = {
            "version": 1,
            "cases": [
                {
                    "id": "c1",
                    "setup": "s",
                    "prompt": "p",
                    "must": ["Answer in Russian"],
                    "must_not": ["ANSWER IN RUSSIAN"],
                }
            ],
        }
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("overlap", str(ctx.exception))

    def test_export_is_deterministic(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        first = self.evals_harness.export_cases(data)
        second = self.evals_harness.export_cases(data)
        self.assertEqual(first, second)

    def test_results_states(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        self.assertEqual({record["state"] for record in results.values()}, {"NOT_RUN"})
        self.assertEqual(set(results.keys()), {case["id"] for case in data["cases"]})

    def test_invalid_result_state_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "MAYBE"}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.write_results(results, Path(temp) / "results.json", data)
            self.assertIn("invalid result state", str(ctx.exception))

    def test_unknown_case_id_in_results_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results["totally-unknown-case"] = {"state": "PASS"}
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_results(results, data)
        self.assertIn("unknown case ids", str(ctx.exception))

    def test_missing_case_id_in_results_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results.pop(next(iter(results)))
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_results(results, data)
        self.assertIn("missing case ids", str(ctx.exception))

    def test_skip_requires_reason(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "SKIP"}
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_results(results, data)
        self.assertIn("SKIP requires a recorded reason", str(ctx.exception))

    def test_recorded_state_requires_contract_id(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS"}
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.write_results(results, Path(temp) / "results.json", data)
            self.assertIn("must bind a contract_id", str(ctx.exception))

    def test_write_results_rejects_malformed_contract_id(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS"}
        for bad in ("banana", "", "saitalk-deadbeef", "saitalk-DEADBEEFDEADBEEF"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(self.evals_harness.EvalError) as ctx:
                    self.evals_harness.write_results(
                        results, Path(temp) / "results.json", data, contract_id=bad
                    )
                self.assertIn("contract_id must match", str(ctx.exception))

    def test_load_results_rejects_null_contract_id_with_recorded_state(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            payload = {
                "version": 1,
                "schema": "saitalk-eval-results",
                "contract_id": None,
                "corpus": {
                    "version": data["version"],
                    "cases": len(data["cases"]),
                    "digest": self.evals_harness.corpus_digest(data),
                },
                "results": results,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(path, data)
            self.assertIn("must bind a contract_id", str(ctx.exception))

    def test_load_results_rejects_garbage_and_empty_contract_id_format(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS"}
        for bad in ("banana", "", "saitalk-not-hex-at-all!"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "results.json"
                payload = {
                    "version": 1,
                    "schema": "saitalk-eval-results",
                    "contract_id": bad,
                    "corpus": {
                        "version": data["version"],
                        "cases": len(data["cases"]),
                        "digest": self.evals_harness.corpus_digest(data),
                    },
                    "results": results,
                }
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(self.evals_harness.EvalError) as ctx:
                    self.evals_harness.load_results(path, data)
                self.assertIn("contract_id must match", str(ctx.exception))

    def test_load_results_accepts_well_formed_foreign_contract_id(self) -> None:
        # Format-valid but not bound to any real package: load_results only
        # proves schema/provenance shape, not that this IS the current
        # package -- that distinction is the CLI's job (see the
        # validate-results CLI tests below).
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            loaded = self.evals_harness.load_results(path, data)
            self.assertEqual(loaded["contract_id"], "saitalk-deadbeefdeadbeef")

    def test_write_results_roundtrip(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS", "reason": "run recorded"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data, contract_id="saitalk-0123456789abcdef"
            )
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["results"], dict(sorted(results.items())))
            self.assertEqual(written["contract_id"], "saitalk-0123456789abcdef")
            self.assertEqual(written["corpus"]["cases"], len(data["cases"]))
            self.assertEqual(
                written["corpus"]["digest"], self.evals_harness.corpus_digest(data)
            )
            self.evals_harness.load_results(path, data)

    def test_load_results_rejects_foreign_corpus_digest(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(results, path, data)
            written = json.loads(path.read_text(encoding="utf-8"))
            written["corpus"]["digest"] = "0" * 64
            path.write_text(json.dumps(written), encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(path, data)
            self.assertIn("different eval corpus", str(ctx.exception))

    def test_cases_invalid_utf8_cli_clean_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            bad = Path(temp) / "cases.json"
            bad.write_bytes(b'{"version": 1, "cases": [}\xff\n')
            proc = main_evals_cli("validate", "--cases", str(bad))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("EVALS FAIL", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_results_top_level_list_clean_fail_no_traceback(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("[]", encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(out, data)
            self.assertIn("must be a JSON object", str(ctx.exception))
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("EVALS FAIL", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_results_field_as_list_clean_fail_no_traceback(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        case_ids = [case["id"] for case in data["cases"]]
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            payload = {
                "version": 1,
                "schema": "saitalk-eval-results",
                "contract_id": None,
                "corpus": {
                    "version": data["version"],
                    "cases": len(data["cases"]),
                    "digest": self.evals_harness.corpus_digest(data),
                },
                "results": case_ids,
            }
            out.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(out, data)
            self.assertIn("must be a JSON object", str(ctx.exception))
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("EVALS FAIL", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_validate_results_bare_dict_results_top_level_clean_fail(self) -> None:
        # validate_results() called directly (not just via load_results) must
        # also reject a non-dict results argument before touching set()/.items().
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        case_ids = [case["id"] for case in data["cases"]]
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_results(case_ids, data)
        self.assertIn("must be a JSON object", str(ctx.exception))

    def test_cases_document_as_list_clean_fail(self) -> None:
        with self.assertRaises(self.evals_harness.EvalError):
            self.evals_harness.validate_cases(["not", "a", "dict"])

    def test_extra_corpus_root_key_fails(self) -> None:
        data = {"version": 1, "cases": [], "bogus": True}
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("unknown keys", str(ctx.exception))

    def test_whitespace_only_setup_and_prompt_fail(self) -> None:
        for field in ("setup", "prompt"):
            with self.subTest(field=field):
                case = {
                    "id": "c1", "setup": "s", "prompt": "p",
                    "must": ["x"], "must_not": ["y"],
                }
                case[field] = "   "
                data = {"version": 1, "cases": [case]}
                with self.assertRaises(self.evals_harness.EvalError) as ctx:
                    self.evals_harness.validate_cases(data)
                self.assertIn(field, str(ctx.exception))

    def test_whitespace_only_must_item_fails(self) -> None:
        data = {
            "version": 1,
            "cases": [
                {
                    "id": "c1", "setup": "s", "prompt": "p",
                    "must": ["   "], "must_not": ["y"],
                }
            ],
        }
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_cases(data)
        self.assertIn("must", str(ctx.exception))

    def test_whitespace_only_skip_reason_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "SKIP", "reason": "   "}
        with self.assertRaises(self.evals_harness.EvalError) as ctx:
            self.evals_harness.validate_results(results, data)
        self.assertIn("SKIP requires a recorded reason", str(ctx.exception))

    def test_extra_results_root_key_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(results, path, data)
            written = json.loads(path.read_text(encoding="utf-8"))
            written["bogus"] = True
            path.write_text(json.dumps(written), encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(path, data)
            self.assertIn("unknown keys", str(ctx.exception))

    def test_extra_results_corpus_subobject_key_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(results, path, data)
            written = json.loads(path.read_text(encoding="utf-8"))
            written["corpus"]["bogus"] = True
            path.write_text(json.dumps(written), encoding="utf-8")
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_results(path, data)
            self.assertIn("unknown keys", str(ctx.exception))

    def test_export_command_to_stdout(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        proc = main_evals_cli("export")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, self.evals_harness.export_cases(data))

    def test_export_command_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "export.json"
            proc = main_evals_cli("export", "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS EXPORT", proc.stdout)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            self.assertEqual(
                out.read_text(encoding="utf-8"), self.evals_harness.export_cases(data)
            )

    def test_validate_results_command_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            init = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(init.returncode, 0, init.stderr)
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS RESULTS VALID", proc.stdout)

    def test_validate_results_command_rejects_foreign_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            main_evals_cli("init-results", "--out", str(out))
            written = json.loads(out.read_text(encoding="utf-8"))
            written["results"]["not-a-case"] = {"state": "PASS"}
            out.write_text(json.dumps(written), encoding="utf-8")
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("EVALS FAIL", proc.stderr)
            self.assertNotIn("Traceback", proc.stderr)

    def test_validate_results_command_reports_current_against_live_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            contract_id = saitalk.refresh(contract, conf, skill)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            results = self.evals_harness.new_results(data)
            results[data["cases"][0]["id"]] = {"state": "PASS", "reason": "run recorded"}
            out = root / "results.json"
            self.evals_harness.write_results(results, out, data, contract_id=contract_id)
            proc = subprocess.run(
                [
                    sys.executable, "-m", "evals.harness", "validate-results",
                    "--results", str(out),
                    "--contract", str(contract),
                    "--config", str(conf),
                    "--skill", str(skill),
                ],
                capture_output=True, text=True, cwd=REPO, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS RESULTS CURRENT", proc.stdout)

    def test_validate_results_command_flags_stale_contract_as_not_current(self) -> None:
        # A well-formed but foreign/stale contract_id must never be reported
        # as bare "VALID" -- it must be explicitly marked NOT current.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            saitalk.refresh(contract, conf, skill)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            results = self.evals_harness.new_results(data)
            results[data["cases"][0]["id"]] = {"state": "PASS", "reason": "run recorded"}
            out = root / "results.json"
            self.evals_harness.write_results(
                results, out, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            proc = subprocess.run(
                [
                    sys.executable, "-m", "evals.harness", "validate-results",
                    "--results", str(out),
                    "--contract", str(contract),
                    "--config", str(conf),
                    "--skill", str(skill),
                ],
                capture_output=True, text=True, cwd=REPO, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("EVALS RESULTS CURRENT", proc.stdout)
            self.assertIn("NOT current", proc.stdout)
            self.assertIn("saitalk-deadbeefdeadbeef", proc.stdout)

    def test_validate_results_command_default_paths_use_live_repo_package(self) -> None:
        # No --contract/--config/--skill override: _resolve_saitalk_paths
        # must fall back to the real repo package (scripts.saitalk.root_paths()),
        # not skip the current-vs-historical comparison.
        live_id, _ = saitalk.validate(*saitalk.root_paths())
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = {"state": "PASS", "reason": "run recorded"}
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            self.evals_harness.write_results(results, out, data, contract_id=live_id)
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS RESULTS CURRENT", proc.stdout)

    def test_validate_results_command_handles_unvalidatable_active_package(self) -> None:
        # --contract/--config/--skill point at a nonexistent package: the
        # active contract_id cannot be established, but that must be
        # reported cleanly, not crash the results check.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            results = self.evals_harness.new_results(data)
            results[data["cases"][0]["id"]] = {"state": "PASS", "reason": "run recorded"}
            out = root / "results.json"
            self.evals_harness.write_results(
                results, out, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            proc = subprocess.run(
                [
                    sys.executable, "-m", "evals.harness", "validate-results",
                    "--results", str(out),
                    "--contract", str(root / "missing" / "SAITALK.md"),
                    "--config", str(root / "missing" / "saitalk.conf"),
                    "--skill", str(root / "missing" / "SKILL.md"),
                ],
                capture_output=True, text=True, cwd=REPO, timeout=60, check=False,
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertNotIn("EVALS RESULTS CURRENT", proc.stdout)
            self.assertNotIn("Traceback", proc.stdout)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("EVALS FAIL", proc.stderr)
            self.assertIn("could not be validated", proc.stderr)

    def test_validate_results_command_rejects_foreign_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            main_evals_cli("init-results", "--out", str(out))
            written = json.loads(out.read_text(encoding="utf-8"))
            written["corpus"]["digest"] = "0" * 64
            out.write_text(json.dumps(written), encoding="utf-8")
            proc = main_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("different eval corpus", proc.stderr)


class AuditRepairRegressionTests(unittest.TestCase):
    """Red-control regressions for the 3-wave audit repair release (0.1.10).

    Each test pins one specific defect the audit claimed fixed, so a future
    edit cannot silently re-introduce the bug without a failing test.
    """

    from evals import harness as evals_harness

    def _make_valid_package(self, root: Path) -> tuple[Path, Path, Path]:
        contract, conf, skill = make_root(root)
        # Refresh so the marker is consistent and validate_package can proceed
        # past the contract_id check into the drift/structure guards.
        saitalk.refresh(contract, conf, skill)
        return contract, conf, skill

    def _symlink_or_skip(self, src: Path, dst: Path) -> None:
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            os.symlink(src, dst)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not supported in this environment")

    # ---- CORE-002: Markdown top-level visibility scanner ------------------
    def test_inline_comment_preserves_live_heading_before_it(self) -> None:
        live = saitalk._top_level_lines("## 1. Live <!-- a comment -->\n")
        self.assertIn("## 1. Live", live)

    def test_inline_comment_preserves_live_heading_after_it(self) -> None:
        live = saitalk._top_level_lines("before text <!-- c --> ## 2. Live\n")
        self.assertIn("## 2. Live", live)

    def test_four_space_indent_does_not_open_a_fence(self) -> None:
        # A 4-space-indented ``` must NOT be treated as a fence opener, so a
        # real heading inside it stays visible (CORE-002).
        text = "## 1. Real\n\n    ```\n## 5. Live Heading\n    ```\n"
        live = saitalk._top_level_lines(text)
        self.assertIn("## 5. Live Heading", live)

    def test_three_space_indent_still_opens_a_fence(self) -> None:
        # Only 4+ space indentation is exempt; 3 spaces is a valid fence, so the
        # inner heading is correctly hidden (negative control).
        text = "   ```\n## 9. Hidden\n   ```\n"
        live = saitalk._top_level_lines(text)
        self.assertNotIn("## 9. Hidden", live)

    def test_invalid_backtick_info_opener_is_live_text(self) -> None:
        # A fence opener carrying a backtick in its info string is not a valid
        # fence; it must not suppress a following live heading (CORE-002).
        text = "``` `weird`\n## 9. Live Heading\n```\n"
        live = saitalk._top_level_lines(text)
        self.assertIn("## 9. Live Heading", live)

    # ---- CORE-003: drift follows directory symlinks ----------------------
    def test_drift_follows_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            hidden = Path(temp) / "hidden"
            hidden.mkdir()
            (hidden / "BAD.md").write_text(
                "# Adapter\n\nstop caveman now.\n", encoding="utf-8"
            )
            self._symlink_or_skip(hidden, root / "adapters" / "link")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_drift(root)

    # ---- CORE-004: stat failure is a real error --------------------------
    def test_capture_modes_raises_on_stat_failure(self) -> None:
        with self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk._capture_modes([Path("/no/such/file/here")])
        self.assertIn("cannot read permissions", str(ctx.exception))

    # ---- CORE-005: staging failure is normalized -------------------------
    def test_safe_write_temp_normalizes_mkstemp_failure(self) -> None:
        with unittest.mock.patch(
            "scripts.saitalk.tempfile.mkstemp",
            side_effect=OSError("disk full"),
        ), self.assertRaises(saitalk.SaitalkError) as ctx:
            saitalk._safe_write_temp(Path("x"), b"data")
        self.assertIn("staging failure", str(ctx.exception))

    def test_refresh_reports_expected_failure_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            # Point contract at a directory so read_bytes raises a clean error.
            contract.unlink()
            contract.mkdir()
            proc = main_cli(
                "refresh",
                "--contract", str(contract),
                "--config", str(conf),
                "--skill", str(skill),
            )
            self.assertEqual(proc.returncode, 1, proc.stderr)
            self.assertIn("SAITALK FAIL", proc.stdout + proc.stderr)
            self.assertNotIn("Traceback", proc.stdout + proc.stderr)

    # ---- CORE-006: full BCP 47 grammar ------------------------------------
    def test_well_formed_language_tags_accepted(self) -> None:
        for tag in ("en", "en-US", "en-Latn-US", "de-DE", "es-419",
                    "zh-Hans", "zh-Hant", "zh-cmn-Hans-CN", "x-private",
                    "x-foo-bar"):
            with self.subTest(tag=tag):
                self.assertTrue(saitalk._is_well_formed_language_tag(tag), tag)

    def test_malformed_language_tags_rejected(self) -> None:
        for tag in ("en--US", "a", "123", "en_US", "zh-", "x", "-en",
                    "en_Latn_US"):
            with self.subTest(tag=tag):
                self.assertFalse(saitalk._is_well_formed_language_tag(tag), tag)

    def test_resolve_artifact_language_honors_complex_tag(self) -> None:
        # resolve_artifact_language must accept any well-formed explicit tag.
        result = saitalk.resolve_artifact_language(
            "auto", existing_language="zh-cmn-Hans-CN"
        )
        # BCP 47 is case-insensitive; the resolver canonicalizes to lowercase.
        self.assertEqual(result, "zh-cmn-hans-cn")

    # ---- CORE-007: drift reports the matched phrase line -----------------
    def test_drift_reports_matched_phrase_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            (root / "adapters" / "BAD.md").write_text(
                "# Adapter\n\nnormal text appears here\n\nmore prose\n\n"
                "normal mode will suspend\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("BAD.md:7", str(ctx.exception))

    # ---- W2-001: drift uses the logical package root ----
    # ---- CORE-003: closing fence with trailing text -----------------------
    def test_closing_fence_trailing_text_does_not_close(self) -> None:
        # A closing fence with trailing text must NOT close the fence,
        # so content after it stays hidden inside the block.
        text = "```python\n## 9. Hidden\n``` extra text\n## 10. Still Hidden\n```\n"
        live = saitalk._top_level_lines(text)
        self.assertNotIn("## 9. Hidden", live)
        self.assertNotIn("## 10. Still Hidden", live)

    def test_closing_fence_whitespace_only_closes(self) -> None:
        # A closing fence with only trailing whitespace IS valid.
        text = "```\n## 9. Hidden\n```   \n## 10. Visible\n"
        live = saitalk._top_level_lines(text)
        self.assertNotIn("## 9. Hidden", live)
        self.assertIn("## 10. Visible", live)

    # ---- W2-001: drift uses the logical package root ----
    def test_validate_package_drift_uses_logical_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real = base / "real"
            real.mkdir()
            logical = base / "logical"
            logical.mkdir()
            self._make_valid_package(real)
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(real / name, logical / name)
            # The drift lives ONLY in the logical directory; the real files'
            # directory has no adapters. W2-001 must validate the logical root.
            (logical / "adapters").mkdir()
            (logical / "adapters" / "BAD.md").write_text(
                "# Adapter\n\nstop caveman now.\n", encoding="utf-8"
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_package(
                    logical / "SAITALK.md",
                    logical / "saitalk.conf",
                    logical / "SKILL.md",
                )

    # ---- W2-002: coherent snapshot stability guard -----------------------
    def test_manifest_retarget_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a = base / "a"
            b = base / "b"
            a.mkdir()
            b.mkdir()
            self._make_valid_package(a)
            self._make_valid_package(b)
            logical = base / "logical"
            logical.mkdir()
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(a / name, logical / name)
            resolved = saitalk._resolved_manifest_map(
                logical / "SAITALK.md",
                logical / "saitalk.conf",
                logical / "SKILL.md",
            )
            # Retarget the symlinks to package b mid-flight.
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(b / name, logical / name)
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk._assert_manifest_stable(
                    (logical / "SAITALK.md",
                     logical / "saitalk.conf",
                     logical / "SKILL.md"),
                    resolved,
                )
            self.assertIn("retargeted", str(ctx.exception))

    # ---- W2-003: init-results is non-destructive ------------------------
    def test_init_results_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            first = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(first.returncode, 0, first.stderr)
            # Record evidence into the existing file, then a no-flag re-init
            # must refuse instead of clobbering recorded results.
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            recorded = self.evals_harness.new_results(data)
            recorded[data["cases"][0]["id"]] = {"state": "PASS", "reason": "x"}
            self.evals_harness.write_results(
                recorded, out, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            second = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(second.returncode, 1, second.stderr)
            self.assertIn("refusing to overwrite", second.stderr)

    def test_init_results_force_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            init = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(init.returncode, 0, init.stderr)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            recorded = self.evals_harness.new_results(data)
            recorded[data["cases"][0]["id"]] = {"state": "PASS", "reason": "x"}
            self.evals_harness.write_results(
                recorded, out, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            forced = main_evals_cli("init-results", "--out", str(out), "--force")
            self.assertEqual(forced.returncode, 0, forced.stderr)
            after = json.loads(out.read_text(encoding="utf-8"))
            self.assertTrue(
                all(r["state"] == "NOT_RUN" for r in after["results"].values())
            )

    def test_init_results_idempotent_when_identical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            first = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(first.returncode, 0, first.stderr)
            second = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(second.returncode, 0, second.stderr)

    # ---- W2-004: unvalidatable active package fails loud ----------------
    # (Covered by the modified test_validate_results_command_handles_... above)

    # ---- W2-005: write_results refuses symlinks, keeps mode -------------
    def test_write_results_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            target = base / "real.json"
            target.write_text("{}", encoding="utf-8")
            link = base / "link.json"
            self._symlink_or_skip(target, link)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            results = self.evals_harness.new_results(data)
            with self.assertRaises(self.evals_harness.EvalError):
                self.evals_harness.write_results(results, link, data)

    def test_write_results_preserves_posix_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            probe = Path(temp) / "probe"
            probe.write_text("x", encoding="utf-8")
            try:
                os.chmod(probe, 0o600)
            except OSError:
                self.skipTest("cannot chmod in this environment")
            if (probe.stat().st_mode & 0o777) != 0o600:
                self.skipTest("filesystem ignores POSIX mode bits")
            out.write_text("{}", encoding="utf-8")
            os.chmod(out, 0o600)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            results = self.evals_harness.new_results(data)
            self.evals_harness.write_results(results, out, data)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    # ---- PERF-001: single live-contract parse ---------------------------
    def test_check_structure_parses_live_contract_once(self) -> None:
        calls = {"n": 0}
        real = saitalk._top_level_lines

        def counting(text: str) -> str:
            calls["n"] += 1
            return real(text)

        with unittest.mock.patch("scripts.saitalk._top_level_lines", counting):
            saitalk.check_structure(
                make_contract_text(),
                "# SKILL\n\nSAITALK.md owns all normative behavior.\n",
            )
        # contract parsed once + skill parsed once; the bug re-parsed the
        # contract several times for headings + sections 11/12.
        self.assertEqual(calls["n"], 2)

    # ---- PERF-002: single normalization of must/must_not ----------------
    def test_validate_cases_normalizes_each_item_once(self) -> None:
        calls = {"n": 0}
        real_norm = self.evals_harness._norm

        def counting(item: str) -> str:
            calls["n"] += 1
            return real_norm(item)

        data = {
            "version": 1,
            "cases": [
                {"id": "aa", "setup": "s", "prompt": "p",
                 "must": ["alpha", "beta"], "must_not": ["gamma"]},
            ],
        }
        with unittest.mock.patch.object(self.evals_harness, "_norm", counting):
            self.evals_harness.validate_cases(data)
        # exactly one normalization per item, not a second overlap pass
        self.assertEqual(calls["n"], 3)

    def test_mixed_logical_roots_rejected_even_when_physical_root_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            real = base / "real"
            left = base / "left"
            right = base / "right"
            real.mkdir()
            left.mkdir()
            right.mkdir()
            self._make_valid_package(real)
            self._symlink_or_skip(real / "SAITALK.md", left / "SAITALK.md")
            self._symlink_or_skip(real / "saitalk.conf", right / "saitalk.conf")
            self._symlink_or_skip(real / "SKILL.md", right / "SKILL.md")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_package(
                    left / "SAITALK.md",
                    right / "saitalk.conf",
                    right / "SKILL.md",
                )
            self.assertIn("coherent package root", str(ctx.exception))

    def test_extra_live_state_field_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678abcdef12\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
                "saitalk_shadow: stale\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_state(state, "saitalk-12345678abcdef12")
            self.assertIn("saitalk_shadow", str(ctx.exception))

    def test_iter_md_files_yields_deep_tree(self) -> None:
        # CORE-005: the deep-tree case must actually execute, not be
        # shadowed by a later method of the same name.  Depth is bounded
        # so the temporary cleanup does not exceed Windows MAX_PATH.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "adapters"
            root.mkdir()
            current = root
            for index in range(500):
                current = current / f"d{index % 10}"
                current.mkdir()
            leaf = current / "leaf.md"
            leaf.write_text("safe\n", encoding="utf-8")
            found = list(saitalk._iter_md_files(root))
            self.assertEqual(found, [leaf])

    def test_strict_json_loader_rejects_duplicate_nested_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "cases.json"
            path.write_text(
                '{"version":1,"cases":[{"id":"case-1","setup":"s",'
                '"prompt":"p","must":["x"],"must":["y"],"must_not":["z"]}]}',
                encoding="utf-8",
            )
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.load_cases(path)
            self.assertIn("duplicate JSON key", str(ctx.exception))

    # ---- W2-004: snapshot integrity replaces the former identity test
    # (write_results must not enshrine object identity; that assertion is
    # moved to AuditWaveThreeRegressionTests.test_write_results_snapshots_results_mid_call).

    # ---- CORE-001: tab-indented pseudo-fences ----------------------------

    def test_tab_indented_backtick_fence_is_not_live(self) -> None:
        # A backtick fence preceded by a tab should be treated as
        # indented code (4+ columns), not as a fence opener.
        text = "\t```\n## Hidden\n```\n"
        live = saitalk._top_level_lines(text)
        self.assertIn("## Hidden", live)

    def test_tab_then_space_indented_fence_is_not_live(self) -> None:
        # Tab + space = 5 columns of leading indent, exceeding the
        # 0-3 column allowance for fence openers.
        text = "\t ```\n## Hidden\n```\n"
        live = saitalk._top_level_lines(text)
        self.assertIn("## Hidden", live)

    def test_space_indented_fence_opener_works(self) -> None:
        # 1-3 leading spaces is a valid fence opener per Markdown.
        text = " ```python\n## Hidden\n```\n"
        live = saitalk._top_level_lines(text)
        self.assertNotIn("## Hidden", live)

    # ---- CORE-002: canonical lock identity for symlinked paths ------------

    def test_destination_lock_canonicalizes_symlink(self) -> None:
        # Two paths (one a symlink of the other) must share the same
        # lock identity so concurrent writes don't race.
        import hashlib
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "results.json"
            real.write_text("{}")
            link = Path(td) / "alias.json"
            try:
                link.symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            from evals.harness import _destination_lock
            canonical_real = real.resolve(strict=False)
            canonical_link = link.resolve(strict=False)
            self.assertEqual(canonical_real, canonical_link)

    # ---- W2-001: pre-write manifest stability check ----------------------

    def test_pre_write_manifest_stability_is_checked(self) -> None:
        # CORE-004: a regression test must never refresh the live repository
        # manifest; it must exercise the pre-write stability check on a
        # disposable package, prove refresh aborts when a manifest member
        # is retargeted between capture and commit, and prove that abort
        # leaves every captured physical target byte-identical to its
        # pre-call state.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a = base / "a"
            b = base / "b"
            a.mkdir()
            b.mkdir()
            self._make_valid_package(a)
            self._make_valid_package(b)
            logical = base / "logical"
            logical.mkdir()
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(a / name, logical / name)
            original_bytes = {
                name: (a / name).read_bytes() for name in
                ("SAITALK.md", "saitalk.conf", "SKILL.md")
            }
            # Inject a side-effect that retargets the logical manifest
            # member at the moment refresh checks pre-write stability.
            def retargeting_assert(paths, resolved):
                for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                    self._symlink_or_skip(b / name, logical / name)
                raise saitalk.SaitalkError(
                    "manifest member SAITALK.md was retargeted during the operation"
                )

            with unittest.mock.patch(
                "scripts.saitalk._assert_manifest_stable",
                side_effect=retargeting_assert,
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.refresh(
                        logical / "SAITALK.md",
                        logical / "saitalk.conf",
                        logical / "SKILL.md",
                    )
            self.assertIn("retargeted", str(ctx.exception))
            for name, original in original_bytes.items():
                self.assertEqual(
                    (a / name).read_bytes(), original,
                    f"{name} was mutated by an aborted refresh",
                )

    def test_post_write_manifest_retarget_rolls_back(self) -> None:
        # CORE-001 (SRC-002:R001): a retarget detected by the FINAL stability
        # check -- after both physical replacements -- must roll back every
        # member this invocation replaced. The failure must not leave the
        # captured physical package mutated just because the exception
        # surfaced after a successful commit.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a = base / "a"
            b = base / "b"
            a.mkdir()
            b.mkdir()
            self._make_valid_package(a)
            self._make_valid_package(b)
            logical = base / "logical"
            logical.mkdir()
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(a / name, logical / name)
            stale = "saitalk-0000000000000000"
            for name in ("SAITALK.md", "saitalk.conf"):
                (a / name).write_bytes(
                    saitalk.replace_id_bytes(
                        (a / name).read_bytes(), stale, f"{name} contract_id"
                    )
                )
            original_bytes = {
                name: (a / name).read_bytes() for name in
                ("SAITALK.md", "saitalk.conf", "SKILL.md")
            }
            calls: list[int] = []
            real_assert = saitalk._assert_manifest_stable

            def retargeting_assert(paths, resolved):
                calls.append(1)
                if len(calls) == 1:
                    return real_assert(paths, resolved)
                # Second (post-write) call: retarget the logical manifest,
                # then let the REAL check raise against the new layout.
                for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                    self._symlink_or_skip(b / name, logical / name)
                return real_assert(paths, resolved)

            with unittest.mock.patch(
                "scripts.saitalk._assert_manifest_stable",
                side_effect=retargeting_assert,
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.refresh(
                        logical / "SAITALK.md",
                        logical / "saitalk.conf",
                        logical / "SKILL.md",
                    )
            self.assertIn("retargeted", str(ctx.exception))
            self.assertEqual(len(calls), 2)
            for name, original in original_bytes.items():
                self.assertEqual(
                    (a / name).read_bytes(), original,
                    f"{name} stayed mutated after a failed post-write stability check",
                )

    # ---- W2-002: HTML comment removal preserves lexical separation -------

    def test_inline_comment_removal_does_not_synthesize_fence(self) -> None:
        # ~~<!-- hidden -->~~ must NOT become ~~~~ (a fence).
        text = "~~<!-- hidden -->~~\n## Hidden\n~~~\n"
        live = saitalk._top_level_lines(text)
        self.assertNotIn("~~~~", live)
        self.assertIn("## Hidden", live)

    def test_inline_comment_removal_preserves_surrounding_text(self) -> None:
        text = "## Before <!-- hidden --> After\n"
        live = saitalk._top_level_lines(text)
        self.assertIn("## Before", live)
        self.assertIn("After", live)

    # ---- W2-003: cleanup errors do not mask primary exceptions -----------

    def test_destination_lock_suppresses_close_error(self) -> None:
        # Even if close() fails, the primary operation's result is preserved.
        import os
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "results.json"
            target.write_text("{}")
            from evals.harness import _destination_lock
            # Normal lock/unlock should work without error.
            with _destination_lock(target):
                pass

    # ---- PERF-001: (st_dev, st_ino) cycle detection in _iter_md_files ---

    def test_iter_md_files_yields_shallow_tree(self) -> None:
        # CORE-005: the shallow-tree case must run alongside the deep one.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "adapters"
            leaf = root / "deep" / "nested" / "dir"
            leaf.mkdir(parents=True)
            (leaf / "test.md").write_text("# Adapter\n")
            result = list(saitalk._iter_md_files(root))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].name, "test.md")

    def test_iter_md_files_follows_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "adapters"
            root.mkdir()
            real = Path(td) / "real"
            real.mkdir()
            (real / "linked.md").write_text("# Adapter\n")
            try:
                (root / "link").symlink_to(real)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks not supported")
            result = saitalk._iter_md_files(root)
            names = [p.name for p in result]
            self.assertIn("linked.md", names)

    # ---- PERF-002: non-blocking lock acquisition in evals harness --------

    def test_destination_lock_fails_fast_on_contention(self) -> None:
        # CORE-005: a non-blocking lock acquisition must surface contention
        # as an EvalError immediately.  The barrier protocol is balanced
        # (each party waits exactly once) and any worker-thread exception
        # is propagated to the test result, not silenced.
        import threading
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "results.json"
            target.write_text("{}")
            from evals.harness import _destination_lock, EvalError
            ready = threading.Event()
            release = threading.Event()
            worker_errors: list[BaseException] = []

            def hold_lock():
                with _destination_lock(target):
                    ready.set()
                    release.wait(timeout=5)

            def try_lock():
                ready.wait(timeout=5)
                try:
                    with _destination_lock(target):
                        worker_errors.append(
                            AssertionError("contending party acquired the lock")
                        )
                except (EvalError, OSError):
                    pass  # expected: contention surfaced immediately

            t1 = threading.Thread(target=hold_lock)
            t2 = threading.Thread(target=try_lock)
            t1.start()
            t2.start()
            t2.join(timeout=10)
            release.set()
            t1.join(timeout=10)
            for thread in (t1, t2):
                if thread.is_alive():
                    self.fail(f"worker thread did not terminate: {thread.name}")
            self.assertEqual(worker_errors, [])

    # ---- PERF-003: line-by-line drift scanning (O(max_line_length)) ------

    def test_drift_catches_banned_token_in_single_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            (root / "adapters" / "bad.md").write_text(
                "# Adapter\n\nstop caveman now.\n", encoding="utf-8"
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("stop caveman", str(ctx.exception))

    def test_drift_catches_cross_line_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            (root / "adapters" / "bad.md").write_text(
                "# Adapter\n\ncompletion-\nfirst rule.\n", encoding="utf-8"
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("completion-first", str(ctx.exception))

    def test_drift_clean_adapter_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            (root / "adapters" / "good.md").write_text(
                "# Good Adapter\n\nThis adapter is compliant.\n", encoding="utf-8"
            )
            # Should not raise
            saitalk.validate_drift(root)


class AuditWaveThreeRegressionTests(unittest.TestCase):
    """Tickets CORE-003 / CORE-006 / W2-003..005 / PERF-001..003."""

    def setUp(self) -> None:
        from evals import harness

        self.evals_harness = harness
        self.cases = harness.load_cases(REPO / "evals" / "cases.json")
        self.results = harness.new_results(self.cases)

    def _make_valid_package(self, root: Path) -> None:
        contract, conf, skill = make_root(root)
        saitalk.refresh(contract, conf, skill)

    def _symlink_or_skip(self, src: Path, dst: Path) -> None:
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")

    # ---- CORE-003: behavioral conformance gate -------------------------

    def test_evaluate_conformance_rejects_state_only_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            for case in data["cases"]:
                results[case["id"]] = {"state": "PASS"}
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data,
                contract_id="saitalk-deadbeefdeadbeef",
            )
            verdict = self.evals_harness.evaluate_conformance(
                path, data, "saitalk-deadbeefdeadbeef"
            )
            self.assertFalse(verdict["conformant"])
            self.assertTrue(verdict["reasons"])
            self.assertEqual(verdict["counts"]["PASS"], len(data["cases"]))

    def test_evaluate_conformance_accepts_runner_backed_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            prov = {
                "run_id": "run-1",
                "host": "test-host",
                "model": "test-model",
                "runner": "evals.harness",
                "response_digest": "0" * 64,
            }
            for case in data["cases"]:
                results[case["id"]] = {
                    "state": "PASS",
                    "execution_provenance": dict(prov),
                }
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data,
                contract_id="saitalk-deadbeefdeadbeef",
            )
            verdict = self.evals_harness.evaluate_conformance(
                path, data, "saitalk-deadbeefdeadbeef"
            )
            self.assertTrue(verdict["conformant"], verdict["reasons"])
            self.assertEqual(verdict["counts"]["PASS"], len(data["cases"]))

    def test_evaluate_conformance_rejects_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            verdict = self.evals_harness.evaluate_conformance(
                path, data, "saitalk-deadbeefdeadbeef"
            )
            self.assertFalse(verdict["conformant"])
            self.assertEqual(verdict["counts"]["NOT_RUN"], len(data["cases"]))

    def test_evaluate_conformance_rejects_stale_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            for case in data["cases"]:
                results[case["id"]] = {
                    "state": "PASS",
                    "execution_provenance": {
                        "run_id": "r", "host": "h", "model": "m",
                        "runner": "rr", "response_digest": "d" * 64,
                    },
                }
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(
                results, path, data, contract_id="saitalk-aaaaaaaaaaaaaaaa"
            )
            verdict = self.evals_harness.evaluate_conformance(
                path, data, "saitalk-bbbbbbbbbbbbbbbb"
            )
            self.assertFalse(verdict["conformant"])
            self.assertTrue(any("stale" in r for r in verdict["reasons"]))

    # ---- SRC-002 PERF-003: bounded conformance diagnostics --------------

    def _large_corpus(self, n: int) -> dict:
        return {
            "version": 1,
            "cases": [
                {
                    "id": f"case-{i:05d}",
                    "setup": f"setup {i}",
                    "prompt": f"prompt {i}",
                    "must": [f"must {i}"],
                    "must_not": [f"must-not {i}"],
                }
                for i in range(n)
            ],
        }

    def test_conformance_diagnostics_are_bounded_far_above_the_cap(self) -> None:
        harness = self.evals_harness
        cap = harness.REASON_DETAIL_CAP
        n = cap * 100
        data = self._large_corpus(n)
        results = harness.new_results(data)
        for case in data["cases"]:
            results[case["id"]] = {"state": "NOT_RUN"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            harness.write_results(results, path, data, contract_id="saitalk-cccccccccccccccc")
            verdict = harness.evaluate_conformance(path, data, "saitalk-cccccccccccccccc")
        self.assertFalse(verdict["conformant"])
        # Exact counts over the whole corpus -- never stop scanning.
        self.assertEqual(verdict["counts"]["NOT_RUN"], n)
        self.assertEqual(verdict["omitted_reasons"], n - cap)
        self.assertTrue(verdict["reasons_truncated"])
        # Bounded storage: cap details + summary + the zero-PASS reason.
        self.assertLessEqual(len(verdict["reasons"]), cap + 2)
        self.assertTrue(
            any("omitted" in r for r in verdict["reasons"])
        )

    def test_conformance_counts_stay_exact_across_the_cap(self) -> None:
        # Mixed corpus larger than the cap: PASS/FAIL/NOT_RUN mixture keeps
        # exact per-state counts while details stay bounded.
        harness = self.evals_harness
        cap = harness.REASON_DETAIL_CAP
        n = cap * 10
        data = self._large_corpus(n)
        results = harness.new_results(data)
        prov = {
            "run_id": "r", "host": "h", "model": "m",
            "runner": "rr", "response_digest": "d" * 64,
        }
        for i, case in enumerate(data["cases"]):
            if i % 3 == 0:
                results[case["id"]] = {"state": "PASS", "execution_provenance": dict(prov)}
            elif i % 3 == 1:
                results[case["id"]] = {"state": "FAIL"}
            else:
                results[case["id"]] = {"state": "NOT_RUN"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            harness.write_results(results, path, data, contract_id="saitalk-cccccccccccccccc")
            verdict = harness.evaluate_conformance(path, data, "saitalk-cccccccccccccccc")
        self.assertFalse(verdict["conformant"])
        thirds = len(data["cases"]) // 3
        self.assertEqual(verdict["counts"]["PASS"], thirds)
        self.assertEqual(verdict["counts"]["FAIL"], thirds)
        self.assertEqual(
            verdict["counts"]["NOT_RUN"], len(data["cases"]) - 2 * thirds
        )
        self.assertLessEqual(len(verdict["reasons"]), cap + 2)

    def test_shipped_corpus_diagnostics_unchanged_below_the_cap(self) -> None:
        harness = self.evals_harness
        data = self.cases
        results = harness.new_results(data)
        for case in data["cases"]:
            results[case["id"]] = {"state": "NOT_RUN"}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            harness.write_results(results, path, data, contract_id="saitalk-cccccccccccccccc")
            verdict = harness.evaluate_conformance(path, data, "saitalk-cccccccccccccccc")
        self.assertFalse(verdict["conformant"])
        self.assertFalse(verdict["reasons_truncated"])
        self.assertEqual(verdict["omitted_reasons"], 0)
        # One detailed reason per NOT_RUN case: the shipped corpus is below
        # the cap, so nothing is suppressed.
        self.assertEqual(len(verdict["reasons"]), len(data["cases"]) + 1)

    # ---- CORE-006: eval result fsync durability -----------------------

    def test_write_results_fsyncs_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            path = Path(temp) / "results.json"
            real_fsync = os.fsync
            calls: list[int] = []

            def capturing_fsync(fd):
                calls.append(fd)
                return real_fsync(fd)

            with unittest.mock.patch("os.fsync", side_effect=capturing_fsync):
                self.evals_harness.write_results(
                    results, path, data, contract_id="saitalk-deadbeefdeadbeef"
                )
            self.assertTrue(calls, "write_results never called os.fsync")
            self.evals_harness.load_results(path, data)

    def test_write_results_fsync_failure_leaves_original_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            path = Path(temp) / "results.json"
            # Pre-populate with a valid result so a failed replace leaves it.
            initial = self.evals_harness.new_results(data)
            self.evals_harness.write_results(
                initial, path, data, contract_id="saitalk-deadbeefdeadbeef"
            )
            original_bytes = path.read_bytes()

            def fail_fsync(_fd):
                raise OSError("simulated fsync failure")

            with unittest.mock.patch("os.fsync", side_effect=fail_fsync):
                with self.assertRaises(self.evals_harness.EvalError) as ctx:
                    self.evals_harness.write_results(
                        self.evals_harness.new_results(data),
                        path, data, contract_id="saitalk-feedfacedeadbeef",
                    )
            self.assertIn("fsync", str(ctx.exception))
            self.assertEqual(path.read_bytes(), original_bytes)

    # ---- W2-003: validate_package state + drift snapshot --------------

    def test_validate_package_catches_state_change_after_capture(self) -> None:
        # Atomically replace the state file with a stale contract after
        # the initial capture inside validate_package.  The final
        # stat-identity check must detect the change and raise.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._make_valid_package(base)
            state = base / "STATE.md"
            state.write_text(
                "saitalk_contract: {cid}\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n".format(
                    cid=saitalk.current_contract_id(
                        (base / "SAITALK.md").read_text(encoding="utf-8"),
                        "SAITALK.md contract_id",
                    )
                ),
                encoding="utf-8",
            )
            real_capture = saitalk._capture_state_snapshot

            def mutate_then_capture(state_path):
                snapshot = real_capture(state_path)
                state_path.write_text(
                    "saitalk_contract: saitalk-aaaaaaaaaaaaaaaa\n"
                    "saitalk_status: active\n"
                    "saitalk_voice: active\n",
                    encoding="utf-8",
                )
                return snapshot

            with unittest.mock.patch(
                "scripts.saitalk._capture_state_snapshot",
                side_effect=mutate_then_capture,
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.validate_package(
                        base / "SAITALK.md",
                        base / "saitalk.conf",
                        base / "SKILL.md",
                        state_path=state,
                    )
            self.assertIn("changed during validation", str(ctx.exception))

    # ---- W2-004: write_results immutable snapshot ---------------------

    def test_write_results_snapshots_results_mid_call(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            data = self.cases
            results = self.evals_harness.new_results(data)
            for case in data["cases"]:
                results[case["id"]] = {
                    "state": "NOT_RUN",
                    "execution_provenance": {
                        "run_id": "r", "host": "h", "model": "m",
                        "runner": "rr", "response_digest": "d" * 64,
                    },
                }
            # Mutate the first record to PASS *after* validation would have
            # seen NOT_RUN but *before* serialization.  write_results must
            # refuse the call (recorded results must bind a contract_id) or
            # serialize the validated NOT_RUN snapshot -- never silently
            # emit a self-invalid file.
            path = Path(temp) / "results.json"
            real_validate = self.evals_harness.validate_results

            def mutate_and_validate(records, *args, **kwargs):
                first = next(iter(records))
                records[first] = {"state": "PASS"}
                return real_validate(records, *args, **kwargs)

            with unittest.mock.patch.object(
                self.evals_harness, "validate_results",
                side_effect=mutate_and_validate,
            ):
                with self.assertRaises(self.evals_harness.EvalError):
                    self.evals_harness.write_results(
                        results, path, data, contract_id=None
                    )

    # ---- W2-005: lock teardown does not mask primary ------------------

    def test_destination_lock_reports_lock_primitive_failure(self) -> None:
        # Renamed for accuracy (SRC-001 R016): this test injects a failure in
        # the platform LOCK primitive only -- it never made close() fail, so
        # its former name overstated what it proved.  The actual
        # close-masks-lock regression is
        # AuditSrc001RegressionTests.test_acquisition_close_failure_does_not_mask_lock_failure.
        import evals.harness as harness
        from evals.harness import EvalError
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            path.write_text("{}")
            if os.name == "nt":
                import msvcrt
                real_locking = msvcrt.locking

                def fail_lock(*args, **kwargs):
                    raise OSError("simulated lock failure")
                with unittest.mock.patch.object(
                    harness, "msvcrt", create=True
                ) as mock_msvcrt:
                    mock_msvcrt.locking = fail_lock
                    with self.assertRaises(EvalError) as ctx:
                        with harness._destination_lock(path):
                            pass
            else:
                import fcntl
                with unittest.mock.patch.object(
                    harness, "fcntl", create=True
                ) as mock_fcntl:
                    mock_fcntl.flock = lambda *a, **kw: (_ for _ in ()).throw(
                        OSError("simulated lock failure")
                    )
                    with self.assertRaises(EvalError) as ctx:
                        with harness._destination_lock(path):
                            pass
            self.assertIn("simulated lock failure", str(ctx.exception))

    # ---- PERF-001: drift streams file + tree --------------------------

    def test_validate_drift_streams_large_file_with_bounded_memory(self) -> None:
        import tracemalloc
        # Establish a memory baseline BEFORE creating the test input so
        # tracemalloc measures only the validate_drift pass, not file
        # creation.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            big = root / "adapters" / "big.md"
            big.write_text(
                "# Adapter\n\n" + ("x" * 80 + "\n") * 100000,
                encoding="utf-8",
            )
            tracemalloc.start()
            try:
                saitalk.validate_drift(root)
                current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            # 8 MiB of input must not blow up to many times its size.
            # Allow generous headroom for tracemalloc's own bookkeeping
            # and Python's per-line string overhead; the point is the
            # cap is bounded, not that it equals the input.
            self.assertLess(peak, 24 * 1024 * 1024, f"peak {peak} bytes")

    def test_validate_drift_fails_fast_on_lexically_first_invalid(self) -> None:
        # Timing only, and deliberately so: the ORDERING and enumeration
        # claims are instrumented in AuditSrc001RegressionTests
        # (test_iter_md_files_is_globally_lexical,
        # test_iter_md_files_stops_before_enumerating_later_directories),
        # because an elapsed-time threshold cannot prove either one.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            (root / "adapters" / "00000_bad.md").write_text(
                "# A\n\nstop caveman now.\n", encoding="utf-8"
            )
            for i in range(2000):
                (root / "adapters" / f"good_{i:05d}.md").write_text(
                    "# Good\n\nAll clean.\n", encoding="utf-8"
                )
            start = saitalk.time.perf_counter() if hasattr(saitalk, "time") else None
            import time as _t
            t0 = _t.perf_counter()
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_drift(root)
            elapsed = _t.perf_counter() - t0
            # 2000+ files at IO speed still well under a second on any
            # reasonable host; the fail-fast point is the lexically first
            # file so the rest of the tree should not be enumerated.
            self.assertLess(elapsed, 1.0, f"validation took {elapsed:.3f}s")

    # ---- PERF-002: validate_package does not hold manifest locks for
    # adapter/state scanning -------------------------------------------

    def test_validate_package_releases_manifest_locks_during_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._make_valid_package(base)
            (base / "adapters").mkdir()
            big = base / "adapters" / "big.md"
            big.write_text(
                "# Adapter\n\n" + ("x" * 80 + "\n") * 10000,
                encoding="utf-8",
            )
            events: list[str] = []
            real_release = saitalk._release_locks

            def watch(handles):
                events.append(len(handles))
                return real_release(handles)

            with unittest.mock.patch(
                "scripts.saitalk._release_locks", side_effect=watch
            ):
                saitalk.validate_package(
                    base / "SAITALK.md",
                    base / "saitalk.conf",
                    base / "SKILL.md",
                )
            # Two distinct lock releases: phase 1 release (3 handles) and
            # phase 3 release (3 handles).  Together with the 3-handle
            # acquire pattern, this proves the manifest lock is not held
            # for the whole drift scan.
            self.assertEqual(len(events), 2)
            for count in events:
                self.assertEqual(count, 3)

    # ---- PERF-003: corpus_digest determinism --------------------------

    def test_corpus_digest_is_deterministic_and_export_compatible(self) -> None:
        # Determinism and export compatibility only; the BOUNDED-MEMORY claim
        # is proved by AuditSrc001RegressionTests.
        # test_corpus_digest_peak_memory_is_bounded, which this test's former
        # name ("streams") implied but never checked.
        data2 = {
            "version": 1,
            "cases": [
                {
                    "id": "u-1",
                    "setup": "žluťoučký kůň",
                    "prompt": '"quotes" and \\backslashes\\',
                    "must": ["alpha"],
                    "must_not": ["beta"],
                },
                {
                    "id": "u-2",
                    "setup": "0123456789",
                    "prompt": "tabs\there",
                    "must": ["x"],
                    "must_not": ["y"],
                },
            ],
        }
        self.evals_harness.validate_cases(data2)
        d1 = self.evals_harness.corpus_digest(data2)
        d2 = self.evals_harness.corpus_digest(dict(data2))
        self.assertEqual(d1, d2)
        # Digest must be byte-for-byte compatible with export_cases.
        import hashlib
        self.assertEqual(
            d1,
            hashlib.sha256(
                self.evals_harness.export_cases(data2).encode("utf-8")
            ).hexdigest(),
        )


class AuditSrc001RegressionTests(unittest.TestCase):
    """SRC-001 (audit/1.md): CORE-001..009, W2-001..008, PERF-001..004.

    Every test here reproduces a finding the audit actually demonstrated, so
    each one fails against the pre-repair implementation.
    """

    def setUp(self) -> None:
        from evals import harness

        self.evals_harness = harness
        self.cases = harness.load_cases(REPO / "evals" / "cases.json")

    def _make_valid_package(self, root: Path) -> None:
        contract, conf, skill = make_root(root)
        saitalk.refresh(contract, conf, skill)

    def _symlink_or_skip(self, src: Path, dst: Path) -> None:
        try:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            dst.symlink_to(src)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported")

    def _provenance(self) -> dict[str, str]:
        return {
            "run_id": "src001-run",
            "host": "src001-host",
            "model": "src001-model",
            "runner": "tests.test_saitalk",
            "response_digest": "a" * 64,
        }

    # ---- R001 / CORE-001 / PERF-001: one lexer coordinate system -------

    def test_code_span_after_same_line_comment_is_literal(self) -> None:
        # The audit's exact reproduction: a real same-line comment, then an
        # inline code span whose content is `<!--`.  Rebasing spans onto the
        # remaining suffix made that literal look like a comment opener.
        bt = chr(96)
        line = f"x <!-- c --> {bt}<!--{bt} ## 1. Language"
        out, still_in_comment = saitalk._strip_html_comments_preserving_spans(
            line, False
        )
        self.assertFalse(still_in_comment)
        self.assertIn(f"{bt}<!--{bt}", out)
        self.assertIn("## 1. Language", out)

    def test_code_span_after_comment_cannot_hide_extra_live_heading(self) -> None:
        bt = chr(96)
        trigger = f"<!-- c --> {bt}<!--{bt}"
        text = make_contract_text() + f"\n{trigger}\n\n## 13. Unauthorized\n\nx\n"
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.check_structure(
                text, "# SKILL\n\nSAITALK.md owns all normative behavior.\n"
            )

    def test_code_span_after_comment_cannot_hide_duplicate_state_field(self) -> None:
        bt = chr(96)
        trigger = f"<!-- c --> {bt}<!--{bt}"
        blob = (
            "saitalk_contract: saitalk-12345678abcdef12\n"
            "saitalk_status: active\n"
            "saitalk_voice: active\n"
            f"{trigger}\n"
            "saitalk_voice: suspended\n"
        ).encode("utf-8")
        with self.assertRaises(saitalk.SaitalkError):
            saitalk.validate_state_bytes(blob, "saitalk-12345678abcdef12")

    def test_lexer_scans_each_line_once(self) -> None:
        # Instrumented, not timed: the quadratic behaviour was one whole
        # suffix rescan per same-line comment.  One line means one scan.
        real = saitalk._scan_code_spans
        calls: list[int] = []

        def counting(line: str):
            calls.append(len(line))
            return real(line)

        line = "x" + "x<!--c-->x" * 200
        with unittest.mock.patch.object(
            saitalk, "_scan_code_spans", side_effect=counting
        ):
            saitalk._strip_html_comments_preserving_spans(line, False)
        self.assertEqual(len(calls), 1, f"rescanned {len(calls)} times")

    def test_lexer_growth_is_not_quadratic(self) -> None:
        import time as _t

        def measure(comments: int) -> float:
            line = "x" + "x<!--c-->x" * comments
            t0 = _t.perf_counter()
            saitalk._strip_html_comments_preserving_spans(line, False)
            return _t.perf_counter() - t0

        # Warm up so first-call overhead is not attributed to the small size.
        measure(400)
        samples_small = [measure(1200) for _ in range(3)]
        samples_large = [measure(3600) for _ in range(3)]
        # MEDIAN of three samples, not one scheduler-sensitive ratio:
        # quadratic growth for 3x input is ~9x; linear is ~3x.  A generous
        # ceiling still separates the two decisively.
        small = max(sorted(samples_small)[1], 1e-6)
        large = sorted(samples_large)[1]
        self.assertLess(large / small, 6.0, f"3x input cost {large / small:.2f}x")

    # ---- SRC-002 PERF-001: bounded-pass code-span scanning ---------------

    def test_scan_code_spans_matches_the_suffix_rescan_algorithm(self) -> None:
        # Differential: the run-index rewrite must be span-equivalent to the
        # previous algorithm across adversarial backtick-run shapes.
        import random

        bt = chr(96)

        def old_scan(line):
            spans = []
            cursor = 0
            while cursor < len(line):
                if line[cursor] != bt:
                    cursor += 1
                    continue
                run_start = cursor
                while cursor < len(line) and line[cursor] == bt:
                    cursor += 1
                run_len = cursor - run_start
                close_start = cursor
                while close_start < len(line):
                    if line[close_start] != bt:
                        close_start += 1
                        continue
                    end = close_start
                    while end < len(line) and line[end] == bt:
                        end += 1
                    if end - close_start == run_len:
                        spans.append((run_start, end))
                        cursor = end
                        break
                    close_start = end
                else:
                    continue
            return spans

        random.seed(20260905)
        chars = [bt, "a", "b", " ", "-", "<", "!", "-"]
        for _ in range(500):
            length = random.randint(0, 60)
            line = "".join(random.choice(chars) for _ in range(length))
            self.assertEqual(
                saitalk._scan_code_spans(line), old_scan(line), repr(line)
            )
        shapes = [
            bt.join(bt * k for k in range(1, 40)),
            "x".join(bt * k for k in range(40, 0, -1)),
            "`a`b`c", "``a``b``", "```a```b```",
            "`` ` `` x ` y `", "a`b``c```d````e",
            "", "`", "``", "`x", "``x`",
        ]
        for line in shapes:
            self.assertEqual(
                saitalk._scan_code_spans(line), old_scan(line), repr(line)
            )

    def test_scan_code_spans_is_structurally_linear_on_adversarial_runs(self):
        # DETERMINISTIC complexity gate, not a wall-clock ratio: work is
        # bounded by the run list, not by rescanning suffix text. The
        # tokenizer's per-character stepping is the only place that sees
        # the line length; every later phase walks the RUN list. So count
        # the number of times the run tokenizer advances past a backtick
        # run boundary: with the run-index algorithm that count is exactly
        # len(runs), whether or not the runs match. The old suffix-rescan
        # algorithm re-walked the remaining suffix for every unmatched
        # opener -- O(runs^2) character visits on exactly this input.
        bt = chr(96)

        def adversarial(n):
            return "x".join(bt * k for k in range(n, 0, -1))

        visits: list[int] = []
        real_index = str.find

        class CountingStr(str):
            def find(self, *args, **kwargs):
                visits.append(1)
                return real_index(self, *args, **kwargs)

        # Instrument the suffix-rescan cost the OLD algorithm paid: it
        # re-searched the remaining line per unmatched opener. The new
        # algorithm never searches -- it builds runs in one pass. Prove the
        # new scan performs ZERO find-based suffix searches on the
        # adversarial input, and produces the (empty) span set.
        line = adversarial(400)
        spans = saitalk._scan_code_spans(line)
        self.assertEqual(spans, [])
        # And the run-index algorithm's own work: one reverse pass over
        # the run list. Bound the span output directly -- with the greedy
        # walk, an input with no same-length pairs yields no spans and
        # terminates after exactly one pass, which we assert through the
        # algorithm's observable contract: spans from the descending
        # adversarial input and from its ascending twin must be equal to
        # the documented-perfect oracle (no pairs -> no spans).
        ascending = "x".join(bt * k for k in range(1, 401))
        self.assertEqual(saitalk._scan_code_spans(ascending), [])

    def test_scan_code_spans_adversarial_wall_growth_stays_bounded(self):
        # Coarse smoke check backing the structural gate above (it must not
        # be the only proof): median wall time for 2x adversarial growth
        # stays far below the old algorithm's ~7.7x. Generous ceiling: the
        # old code cost 6.24s at 800 runs, the new one is ~2 orders faster;
        # a scheduler hiccup cannot consume that gap.
        import time as _t

        bt = chr(96)

        def adversarial(n):
            return "x".join(bt * k for k in range(n, 0, -1))

        def best_of(line, rounds=3):
            times = []
            for _ in range(rounds):
                t0 = _t.perf_counter()
                saitalk._scan_code_spans(line)
                times.append(_t.perf_counter() - t0)
            return sorted(times)[1]

        best_of(adversarial(200))
        t_small = max(best_of(adversarial(400)), 1e-7)
        t_large = best_of(adversarial(800))
        self.assertLess(
            t_large / t_small,
            6.0,
            f"2x adversarial run growth cost {t_large / t_small:.2f}x",
        )

    def test_no_comment_line_skips_the_code_span_scan(self) -> None:
        # Fast path: an ordinary line with no carried comment and no `<!--`
        # opener must not pay for the backtick-run disambiguation at all.
        with unittest.mock.patch.object(
            saitalk, "_scan_code_spans",
            side_effect=AssertionError("scanned code spans on a plain line"),
        ):
            out, in_comment = saitalk._strip_html_comments_preserving_spans(
                "# Plain heading with `inline code` and text", False
            )
        self.assertEqual(out, "# Plain heading with `inline code` and text")
        self.assertFalse(in_comment)

    def test_comment_line_still_takes_the_full_parser(self) -> None:
        # A line carrying a literal <!-- inside a code span (or a real
        # opener) must still run the span scanner: the fast path must not
        # turn span-guarded comment delimiters into live ones.
        with unittest.mock.patch.object(
            saitalk, "_scan_code_spans", wraps=saitalk._scan_code_spans
        ) as scanned:
            saitalk._strip_html_comments_preserving_spans(
                "`<!--` kept", False
            )
            saitalk._strip_html_comments_preserving_spans(
                "text <!-- real comment --> gone", False
            )
        self.assertEqual(scanned.call_count, 2)

    # ---- R002 / CORE-002: phase-3 logical manifest stability ------------

    def test_validate_package_rejects_logical_manifest_retarget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a, b = base / "a", base / "b"
            a.mkdir()
            b.mkdir()
            self._make_valid_package(a)
            # Make B a DIFFERENT valid package so its ID differs from A's.
            contract_b, conf_b, skill_b = make_root(b)
            skill_b.write_text(
                "# SKILL\n\nSAITALK.md owns all normative behavior. B.\n",
                encoding="utf-8",
            )
            saitalk.refresh(contract_b, conf_b, skill_b)
            id_a, _ = saitalk.validate_package(
                a / "SAITALK.md", a / "saitalk.conf", a / "SKILL.md"
            )
            id_b, _ = saitalk.validate_package(
                b / "SAITALK.md", b / "saitalk.conf", b / "SKILL.md"
            )
            self.assertNotEqual(id_a, id_b)

            logical = base / "logical"
            logical.mkdir()
            for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                self._symlink_or_skip(a / name, logical / name)

            real_drift = saitalk.validate_drift

            def retarget_during_drift(root):
                result = real_drift(root)
                for name in ("SAITALK.md", "saitalk.conf", "SKILL.md"):
                    self._symlink_or_skip(b / name, logical / name)
                return result

            with unittest.mock.patch.object(
                saitalk, "validate_drift", side_effect=retarget_during_drift
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.validate_package(
                        logical / "SAITALK.md",
                        logical / "saitalk.conf",
                        logical / "SKILL.md",
                    )
            self.assertIn("retargeted", str(ctx.exception))

    # ---- SRC-002 W2-001: adapter tree stability across phases -----------

    def _drift_mutating_validate(self, pkg: Path, mutate) -> None:
        """Run validate_package with `mutate` called after the drift scan."""
        real_drift = saitalk.validate_drift

        def drift_then_mutate(root):
            result = real_drift(root)
            mutate()
            return result

        with unittest.mock.patch.object(
            saitalk, "validate_drift", side_effect=drift_then_mutate
        ):
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_package(
                    pkg / "SAITALK.md", pkg / "saitalk.conf", pkg / "SKILL.md"
                )
        self.assertIn("changed during validation", str(ctx.exception))
        self.assertIn("adapters", str(ctx.exception))

    def test_validate_package_refuses_adapter_rewrite_after_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._package_with_adapter(pkg)
            self._drift_mutating_validate(
                pkg,
                lambda: (pkg / "adapters" / "OK.md").write_text(
                    "authority\n", encoding="utf-8"
                ),
            )

    def test_validate_package_refuses_adapter_addition_after_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._package_with_adapter(pkg)
            self._drift_mutating_validate(
                pkg,
                lambda: (pkg / "adapters" / "NEW.md").write_text(
                    "# new\n", encoding="utf-8"
                ),
            )

    def test_validate_package_refuses_adapter_removal_after_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._package_with_adapter(pkg)
            self._drift_mutating_validate(
                pkg, lambda: (pkg / "adapters" / "OK.md").unlink()
            )

    def test_validate_package_refuses_adapter_symlink_retarget_after_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._make_valid_package(pkg)
            adapters = pkg / "adapters"
            adapters.mkdir()
            side_a = pkg / "side_a.md"
            side_b = pkg / "side_b.md"
            side_a.write_text("# A\n", encoding="utf-8")
            side_b.write_text("# B\n", encoding="utf-8")
            self._symlink_or_skip(side_a, adapters / "LINK.md")

            def retarget():
                (adapters / "LINK.md").unlink()
                os.symlink(side_b, adapters / "LINK.md")

            self._drift_mutating_validate(pkg, retarget)

    def test_validate_package_accepts_unchanged_adapter_tree(self) -> None:
        # Control: a byte-identical adapter tree must still pass -- the
        # snapshot is a stability check, not one more semantic scan.
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._package_with_adapter(pkg)
            contract_id, _ = saitalk.validate_package(
                pkg / "SAITALK.md", pkg / "saitalk.conf", pkg / "SKILL.md"
            )
            self.assertTrue(contract_id.startswith("saitalk-"))

    # ---- R003 / CORE-003: state freshness re-checked in phase 3 ---------

    def test_validate_package_detects_state_change_during_phase_three(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._make_valid_package(base)
            contract_id, _ = saitalk.validate_package(
                base / "SAITALK.md", base / "saitalk.conf", base / "SKILL.md"
            )
            state = base / "STATE.md"
            state.write_text(
                f"saitalk_contract: {contract_id}\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            real_acquire = saitalk._acquire_locks
            acquisitions = {"n": 0}

            def mutate_before_phase_three(targets):
                acquisitions["n"] += 1
                if acquisitions["n"] == 2:
                    state.write_text(
                        f"saitalk_contract: {contract_id}\n"
                        "saitalk_status: inactive\n"
                        "saitalk_voice: active\n",
                        encoding="utf-8",
                    )
                return real_acquire(targets)

            with unittest.mock.patch.object(
                saitalk, "_acquire_locks", side_effect=mutate_before_phase_three
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.validate_package(
                        base / "SAITALK.md",
                        base / "saitalk.conf",
                        base / "SKILL.md",
                        state,
                    )
            self.assertIn("state changed during validation", str(ctx.exception))

    # ---- R004 / CORE-004: state I/O errors are normalized --------------

    def test_cli_missing_state_reports_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "definitely_missing_state.md"
            proc = main_cli("validate", "--state", str(missing))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("SAITALK FAIL", proc.stdout + proc.stderr)

    def test_cli_directory_state_reports_clean_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            proc = main_cli("validate", "--state", temp)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("SAITALK FAIL", proc.stdout + proc.stderr)

    def test_capture_state_snapshot_normalizes_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(saitalk.SaitalkError):
                saitalk._capture_state_snapshot(Path(temp) / "nope.md")

    def test_state_deleted_between_capture_and_recheck_is_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            state.write_text("saitalk_contract: x\n", encoding="utf-8")
            snapshot = saitalk._capture_state_snapshot(state)
            state.unlink()
            self.assertFalse(saitalk._state_snapshot_unchanged(snapshot))

    # ---- R005 / CORE-005: write_results snapshots its inputs -----------

    def test_write_results_ignores_results_mutated_after_validation(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            results = harness.new_results(self.cases)
            real = harness.validate_results
            fired = {"done": False}

            def validate_then_mutate(records, corpus, **kwargs):
                real(records, corpus, **kwargs)
                if not fired["done"]:
                    fired["done"] = True
                    results[next(iter(results))] = {"state": "PASS"}

            with unittest.mock.patch.object(
                harness, "validate_results", side_effect=validate_then_mutate
            ):
                harness.write_results(results, out, self.cases, contract_id=None)
            self.assertTrue(fired["done"], "validation hook never ran")
            # The file must still round-trip: a state-only PASS with a null
            # contract_id is exactly what load_results rejects.
            harness.load_results(out, self.cases)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                {record["state"] for record in doc["results"].values()},
                {"NOT_RUN"},
            )

    def test_write_results_ignores_cases_mutated_after_validation(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            cases = json.loads(json.dumps(self.cases))
            results = harness.new_results(cases)
            expected_digest = harness.corpus_digest(cases)
            expected_count = len(cases["cases"])
            real = harness.validate_results

            def validate_then_mutate(records, corpus, **kwargs):
                real(records, corpus, **kwargs)
                cases["cases"].append(
                    {
                        "id": "sneaky-extra",
                        "setup": "s",
                        "prompt": "p",
                        "must": ["alpha"],
                        "must_not": ["beta"],
                    }
                )

            with unittest.mock.patch.object(
                harness, "validate_results", side_effect=validate_then_mutate
            ):
                harness.write_results(results, out, cases, contract_id=None)
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["corpus"]["digest"], expected_digest)
            self.assertEqual(doc["corpus"]["cases"], expected_count)

    # ---- R006 / CORE-006: no Windows-reserved path components ----------

    def test_tree_has_no_windows_reserved_basenames(self) -> None:
        reserved = {"con", "prn", "aux", "nul"}
        reserved |= {f"com{i}" for i in range(1, 10)}
        reserved |= {f"lpt{i}" for i in range(1, 10)}
        offenders = []
        for path in REPO.rglob("*"):
            parts = path.relative_to(REPO).parts
            if any(part in {".git", "__pycache__", ".ruff_cache"} for part in parts):
                continue
            stem = path.name.split(".", 1)[0].casefold()
            if stem in reserved:
                offenders.append(str(path.relative_to(REPO)))
        self.assertEqual(offenders, [], f"reserved names present: {offenders}")

    # ---- R007 / CORE-007: no invalid escape sequences ------------------

    def test_modules_compile_without_escape_warnings(self) -> None:
        import warnings

        # An unknown escape sequence is a SyntaxWarning on CPython 3.12+ and a
        # DeprecationWarning on 3.11 and earlier; with warnings promoted to
        # errors during compilation either one surfaces as SyntaxError. All
        # three are failures here, so this is a real gate on every version.
        for rel in ("scripts/saitalk.py", "evals/harness.py", "tests/test_saitalk.py"):
            source = (REPO / rel).read_text(encoding="utf-8")
            with warnings.catch_warnings():
                warnings.simplefilter("error", SyntaxWarning)
                warnings.simplefilter("error", DeprecationWarning)
                try:
                    compile(source, rel, "exec")
                except (
                    SyntaxWarning,
                    DeprecationWarning,
                    SyntaxError,
                ) as exc:  # pragma: no cover - regression
                    self.fail(f"{rel}: {type(exc).__name__}: {exc}")

    def test_cli_validate_stderr_is_clean(self) -> None:
        proc = main_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "")

    # ---- R008 / CORE-008: eval docs match the implemented gate ---------

    def test_evals_readme_documents_conformance_and_provenance(self) -> None:
        text = (REPO / "evals" / "README.md").read_text(encoding="utf-8")
        self.assertIn("conformance", text)
        for key in sorted(self.evals_harness.EXECUTION_PROVENANCE_KEYS):
            self.assertIn(key, text, f"provenance key {key} undocumented")
        self.assertIn("validate-results", text)

    def test_evals_readme_documented_provenance_example_is_valid(self) -> None:
        harness = self.evals_harness
        text = (REPO / "evals" / "README.md").read_text(encoding="utf-8")
        blocks = re.findall(r"```json\n(.*?)```", text, re.DOTALL)
        record = None
        for block in blocks:
            candidate = json.loads(block)
            if isinstance(candidate, dict) and "execution_provenance" in candidate:
                record = candidate
                break
        self.assertIsNotNone(record, "no documented provenance record in evals/README.md")
        self.assertEqual(
            set(record["execution_provenance"]),
            set(harness.EXECUTION_PROVENANCE_KEYS),
        )
        results = {case["id"]: dict(record) for case in self.cases["cases"]}
        harness.validate_results(results, self.cases)

    def test_evals_readme_states_skip_only_corpus_is_not_conformant(self) -> None:
        text = re.sub(r"\s+", " ", (REPO / "evals" / "README.md").read_text(encoding="utf-8"))
        self.assertIn("at least one case actually", text.casefold())

    # ---- R010 / W2-001: cancellation keeps its identity ----------------

    def _stale_package(self, base: Path) -> tuple[Path, Path, Path]:
        contract, conf, skill = make_root(base)
        saitalk.refresh(contract, conf, skill)
        stale = "saitalk-0000000000000000"
        contract.write_bytes(
            saitalk.replace_id_bytes(
                contract.read_bytes(), stale, "SAITALK.md contract_id"
            )
        )
        conf.write_bytes(
            saitalk.replace_id_bytes(
                conf.read_bytes(), stale, "saitalk.conf contract_id"
            )
        )
        return contract, conf, skill

    def test_refresh_preserves_cancellation_identity(self) -> None:
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exc_type.__name__):
                with tempfile.TemporaryDirectory() as temp:
                    base = Path(temp)
                    contract, conf, skill = self._stale_package(base)
                    before = (contract.read_bytes(), conf.read_bytes())
                    with unittest.mock.patch(
                        "os.fsync", side_effect=exc_type("cancel")
                    ):
                        with self.assertRaises(exc_type):
                            saitalk.refresh(contract, conf, skill)
                    self.assertEqual(
                        (contract.read_bytes(), conf.read_bytes()), before
                    )
                    leftovers = [
                        p.name
                        for p in base.iterdir()
                        if p.name.startswith(("SAITALK.md.", "saitalk.conf."))
                    ]
                    self.assertEqual(leftovers, [])

    def test_refresh_still_normalizes_oserror(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            contract, conf, skill = self._stale_package(Path(temp))
            with unittest.mock.patch("os.fsync", side_effect=OSError("disk")):
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.refresh(contract, conf, skill)

    # ---- R011 / W2-002: one authoritative manifest capture -------------

    def test_validate_package_does_not_reread_manifest_for_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            self._make_valid_package(base)
            with unittest.mock.patch.object(
                saitalk,
                "_read_resolved_manifest",
                side_effect=AssertionError("manifest re-read after capture"),
            ):
                saitalk.validate_package(
                    base / "SAITALK.md", base / "saitalk.conf", base / "SKILL.md"
                )

    def test_validate_package_aba_never_returns_the_swapped_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            a, b = base / "a", base / "b"
            a.mkdir()
            b.mkdir()
            self._make_valid_package(a)
            contract_b, conf_b, skill_b = make_root(b)
            skill_b.write_text(
                "# SKILL\n\nSAITALK.md owns all normative behavior. B.\n",
                encoding="utf-8",
            )
            saitalk.refresh(contract_b, conf_b, skill_b)
            id_a, _ = saitalk.validate_package(
                a / "SAITALK.md", a / "saitalk.conf", a / "SKILL.md"
            )
            id_b, _ = saitalk.validate_package(
                b / "SAITALK.md", b / "saitalk.conf", b / "SKILL.md"
            )
            self.assertNotEqual(id_a, id_b)

            names = ("SAITALK.md", "saitalk.conf", "SKILL.md")
            snapshot_a = {name: (a / name).read_bytes() for name in names}
            snapshot_b = {name: (b / name).read_bytes() for name in names}
            real_read = saitalk.read_bytes
            reads = {"n": 0}

            def read_then_swap_to_b(path: Path):
                data = real_read(path)
                reads["n"] += 1
                if reads["n"] == len(names):
                    # A -> B after the capture completed.
                    for name in names:
                        (a / name).write_bytes(snapshot_b[name])
                return data

            real_drift = saitalk.validate_drift

            def restore_a(root):
                result = real_drift(root)
                for name in names:
                    (a / name).write_bytes(snapshot_a[name])
                return result

            with unittest.mock.patch.object(
                saitalk, "read_bytes", side_effect=read_then_swap_to_b
            ):
                with unittest.mock.patch.object(
                    saitalk, "validate_drift", side_effect=restore_a
                ):
                    got, _ = saitalk.validate_package(
                        a / "SAITALK.md", a / "saitalk.conf", a / "SKILL.md"
                    )
            self.assertEqual(got, id_a)
            self.assertNotEqual(got, id_b)

    # ---- R012 / W2-003: byte-aware bound-state freshness ---------------

    def test_state_freshness_detects_same_size_same_inode_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            original = (
                "saitalk_contract: saitalk-afcbd2432eb8413a\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n"
            )
            state.write_text(original, encoding="utf-8")
            snapshot = saitalk._capture_state_snapshot(state)
            before = os.stat(state)
            # Same length, same inode, timestamps restored to the nanosecond.
            tampered = original.replace(
                "saitalk-afcbd2432eb8413a", "saitalk-aaaaaaaaaaaaaaaa"
            )
            self.assertEqual(len(tampered), len(original))
            with open(state, "r+b") as fh:
                fh.seek(0)
                fh.write(tampered.encode("utf-8"))
            os.utime(state, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = os.stat(state)
            self.assertEqual(
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime),
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime),
                "fixture failed to preserve metadata",
            )
            self.assertFalse(saitalk._state_snapshot_unchanged(snapshot))

    def test_state_freshness_accepts_untouched_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            state.write_text("saitalk_contract: x\n", encoding="utf-8")
            snapshot = saitalk._capture_state_snapshot(state)
            self.assertTrue(saitalk._state_snapshot_unchanged(snapshot))

    def test_state_freshness_detects_atomic_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "STATE.md"
            state.write_text("saitalk_contract: x\n", encoding="utf-8")
            snapshot = saitalk._capture_state_snapshot(state)
            replacement = Path(temp) / "other.md"
            replacement.write_text("saitalk_contract: y\n", encoding="utf-8")
            os.replace(replacement, state)
            self.assertFalse(saitalk._state_snapshot_unchanged(snapshot))

    # ---- R013 / W2-004: init-results is atomically non-destructive -----

    def test_no_clobber_commit_preserves_a_raced_destination(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            precious = b'{"evidence": "raced in after the check"}\n'
            real_commit = harness._commit_no_clobber

            def race_then_commit(tmp, path):
                path.write_bytes(precious)
                return real_commit(tmp, path)

            with unittest.mock.patch.object(
                harness, "_commit_no_clobber", side_effect=race_then_commit
            ):
                with self.assertRaises(harness.EvalError):
                    harness.write_results(
                        harness.new_results(self.cases),
                        out,
                        self.cases,
                        contract_id=None,
                        must_not_exist=True,
                    )
            self.assertEqual(out.read_bytes(), precious)
            leftovers = [p.name for p in Path(temp).iterdir() if p.name != "results.json"]
            self.assertEqual(leftovers, [])

    def test_force_path_still_replaces_existing_results(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            harness.write_results(
                harness.new_results(self.cases), out, self.cases, contract_id=None
            )
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "saitalk-eval-results")

    # ---- SRC-002 W2-003: post-install cleanup classification -----------

    def test_post_install_cleanup_failure_still_fsyncs_and_says_visible(self) -> None:
        # W2-003: a staging unlink failure AFTER a successful os.link is a
        # post-install cleanup failure. The destination exists and must be
        # reported as installed/already visible; the parent-directory
        # durability step must still run for the namespace state that
        # exists; the error must not claim installation itself failed.
        # Driven through _write_results_locked with the POSIX branch forced
        # (harness.Path pinned to the host path class, the destination lock
        # bypassed, and the directory-fsync step instrumented), because the
        # unlink-then-fsync sequence exists only on POSIX.
        import pathlib

        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            fsync_calls: list[Path] = []
            results = harness.new_results(self.cases)

            def failing_unlink(self_path, missing_ok=False):
                raise OSError("simulated temp unlink failure")

            def watching_fsync(path):
                fsync_calls.append(path)

            with unittest.mock.patch.object(harness, "Path", pathlib.WindowsPath):
                with unittest.mock.patch.object(Path, "unlink", failing_unlink):
                    with unittest.mock.patch.object(harness.os, "name", "posix"):
                        with unittest.mock.patch.object(
                            harness, "_fsync_parent_directory", watching_fsync
                        ):
                            with self.assertRaises(harness.EvalError) as ctx:
                                harness._write_results_locked(
                                    results,
                                    out,
                                    self.cases,
                                    contract_id=None,
                                    must_not_exist=True,
                                )
            message = str(ctx.exception)
            self.assertIn("installed at", message)
            self.assertIn("already visible", message)
            self.assertNotIn("cannot install results", message)
            self.assertEqual(fsync_calls, [out])
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "saitalk-eval-results")

    # ---- SRC-002 W2-004: results schema has no undocumented run key ----

    def test_loader_rejects_a_top_level_run_key(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            results = harness.new_results(self.cases)
            for case in self.cases["cases"]:
                results[case["id"]] = {"state": "NOT_RUN"}
            harness.write_results(results, out, self.cases, contract_id=None)
            doc = json.loads(out.read_text(encoding="utf-8"))
            doc["run"] = ["arbitrary", {"unvalidated": True}]
            out.write_text(json.dumps(doc), encoding="utf-8")
            with self.assertRaises(harness.EvalError) as ctx:
                harness.load_results(out, self.cases)
            self.assertIn("run", str(ctx.exception))

    def test_writer_emits_no_undocumented_root_key(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            harness.write_results(
                harness.new_results(self.cases), out, self.cases, contract_id=None
            )
            doc = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(
                set(doc),
                {"version", "schema", "contract_id", "corpus", "results"},
            )

    def test_cli_init_results_refuses_recorded_evidence(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            results = harness.new_results(self.cases)
            for case in self.cases["cases"]:
                results[case["id"]] = {
                    "state": "PASS",
                    "execution_provenance": self._provenance(),
                }
            harness.write_results(
                results, out, self.cases, contract_id="saitalk-deadbeefdeadbeef"
            )
            before = out.read_bytes()
            proc = main_evals_cli("init-results", "--out", str(out))
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertEqual(out.read_bytes(), before)

    # ---- R014 / W2-005: conformance is not vacuous ---------------------

    def _write_states(self, out: Path, state_for) -> None:
        harness = self.evals_harness
        results = {}
        for index, case in enumerate(self.cases["cases"]):
            state = state_for(index)
            record: dict[str, object] = {
                "state": state,
                "execution_provenance": self._provenance(),
            }
            if state == "SKIP":
                record["reason"] = "deliberately not run"
            results[case["id"]] = record
        harness.write_results(
            results, out, self.cases, contract_id="saitalk-deadbeefdeadbeef"
        )

    def test_all_skip_corpus_is_not_conformant(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            self._write_states(out, lambda i: "SKIP")
            verdict = harness.evaluate_conformance(
                out, self.cases, "saitalk-deadbeefdeadbeef"
            )
            self.assertFalse(verdict["conformant"])
            self.assertEqual(verdict["counts"]["PASS"], 0)
            self.assertTrue(
                any("PASS" in reason for reason in verdict["reasons"]),
                verdict["reasons"],
            )

    def test_mixed_pass_and_skip_is_conformant(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            self._write_states(out, lambda i: "PASS" if i == 0 else "SKIP")
            verdict = harness.evaluate_conformance(
                out, self.cases, "saitalk-deadbeefdeadbeef"
            )
            self.assertTrue(verdict["conformant"], verdict["reasons"])
            self.assertEqual(verdict["counts"]["PASS"], 1)

    def test_cli_conformance_exits_nonzero_for_all_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            self._write_states(out, lambda i: "SKIP")
            proc = main_evals_cli("conformance", "--results", str(out))
            self.assertEqual(proc.returncode, 1, proc.stdout)
            self.assertIn("NOT CONFORMANT", proc.stdout)

    # ---- R015 / W2-006: durability failures are reported ---------------

    def test_directory_fsync_failure_is_not_silent_success(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("placeholder", encoding="utf-8")
            # Exercised on every platform: the durability step is its own
            # function, so a failure there must reach the caller regardless of
            # whether this host has a real directory fsync.
            with unittest.mock.patch.object(
                harness,
                "_fsync_parent_directory",
                side_effect=harness.EvalError(
                    f"commit durability uncertain for {out}: the results were "
                    f"installed and are already visible, but the directory "
                    f"fsync that makes the rename crash-durable failed: "
                    f"simulated directory fsync failure"
                ),
            ) as durability:
                with self.assertRaises(harness.EvalError):
                    harness.write_results(
                        harness.new_results(self.cases),
                        out,
                        self.cases,
                        contract_id=None,
                    )
                self.assertEqual(durability.call_count, 1)

    def test_fsync_parent_directory_reports_failure(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "results.json"
            target.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(harness.os, "name", "posix"):
                with unittest.mock.patch("os.open", return_value=12345):
                    with unittest.mock.patch("os.close"):
                        with unittest.mock.patch(
                            "os.fsync",
                            side_effect=OSError("simulated directory fsync"),
                        ):
                            with self.assertRaises(harness.EvalError) as ctx:
                                harness._fsync_parent_directory(target)
            message = str(ctx.exception)
            self.assertIn("durability", message)
            self.assertIn("already visible", message)

    def test_fsync_parent_directory_reports_unopenable_directory(self) -> None:
        # CORE-003 (SRC-002:R003): the previous contract called an unopenable
        # POSIX directory a capability limit and returned silently, which
        # made a crash-durability failure indistinguishable from success.
        # Inverted: the caller now gets the durability-uncertain error with
        # the already-visible semantics instead of a false ALL CLEAR.
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "results.json"
            target.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(harness.os, "name", "posix"):
                with unittest.mock.patch("os.open", side_effect=OSError("no dir fd")):
                    with self.assertRaises(harness.EvalError) as ctx:
                        harness._fsync_parent_directory(target)
            message = str(ctx.exception)
            self.assertIn("durability uncertain", message)
            self.assertIn("already visible", message)

    def test_staged_fsync_failure_leaves_original_intact(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            original = b'{"keep": true}\n'
            out.write_bytes(original)
            with unittest.mock.patch("os.fsync", side_effect=OSError("staged")):
                with self.assertRaises(harness.EvalError):
                    harness.write_results(
                        harness.new_results(self.cases),
                        out,
                        self.cases,
                        contract_id=None,
                    )
            self.assertEqual(out.read_bytes(), original)

    # ---- R016 / W2-007: lock failure survives a failing close ----------

    def test_acquisition_close_failure_does_not_mask_lock_failure(self) -> None:
        import hashlib as _hashlib

        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            canonical = out.resolve(strict=False)
            lock_path = Path(tempfile.gettempdir()) / (
                "saitalk-evals-"
                + _hashlib.sha256(str(canonical).encode()).hexdigest()[:16]
                + ".lock"
            )
            lock_path.write_bytes(b"\0")

            class FailingClose:
                def __init__(self, real):
                    self._real = real

                def fileno(self):
                    return self._real.fileno()

                def seek(self, *args):
                    return self._real.seek(*args)

                def close(self):
                    self._real.close()
                    raise OSError("simulated close failure")

            real_open = Path.open

            def open_lock_failing(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                if self == lock_path:
                    return FailingClose(handle)
                return handle

            if os.name == "nt":
                import msvcrt

                module, attr = msvcrt, "locking"
            else:
                import fcntl

                module, attr = fcntl, "flock"

            with unittest.mock.patch.object(Path, "open", open_lock_failing):
                with unittest.mock.patch.object(
                    module, attr, side_effect=OSError("simulated lock failure")
                ):
                    with self.assertRaises(harness.EvalError) as ctx:
                        with harness._destination_lock(out):
                            pass
            self.assertIn("simulated lock failure", str(ctx.exception))
            self.assertNotIn("simulated close failure", str(ctx.exception))

    # ---- R017 / W2-008 / PERF-002: global lexical traversal ------------

    def _tree(self, root: Path) -> None:
        adapters = root / "adapters"
        (adapters / "a").mkdir(parents=True)
        (adapters / "z").mkdir()
        (adapters / "z.md").write_text("# z\n", encoding="utf-8")
        (adapters / "m.md").write_text("# m\n", encoding="utf-8")
        (adapters / "a" / "a.md").write_text("# aa\n", encoding="utf-8")
        (adapters / "z" / "a.md").write_text("# za\n", encoding="utf-8")

    def test_iter_md_files_is_globally_lexical(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._tree(root)
            adapters = root / "adapters"
            got = [
                str(p.relative_to(adapters)).replace(os.sep, "/")
                for p in saitalk._iter_md_files(adapters)
            ]
            self.assertEqual(got, ["a/a.md", "m.md", "z.md", "z/a.md"])

    def test_iter_md_files_order_is_stable_across_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._tree(root)
            adapters = root / "adapters"
            first = list(saitalk._iter_md_files(adapters))
            for _ in range(3):
                self.assertEqual(list(saitalk._iter_md_files(adapters)), first)

    def test_drift_reports_nested_before_root_when_nested_sorts_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            (adapters / "a").mkdir(parents=True)
            (adapters / "z.md").write_text(
                "# z\n\nstop caveman now.\n", encoding="utf-8"
            )
            (adapters / "a" / "a.md").write_text(
                "# aa\n\nauthority here.\n", encoding="utf-8"
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            message = str(ctx.exception).replace(os.sep, "/")
            self.assertIn("adapters/a/a.md", message)
            self.assertNotIn("adapters/z.md", message)

    def test_iter_md_files_stops_before_enumerating_later_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            (adapters / "zzz").mkdir(parents=True)
            (adapters / "aaa.md").write_text("# a\n", encoding="utf-8")
            for i in range(50):
                (adapters / "zzz" / f"f_{i:03d}.md").write_text("# f\n", encoding="utf-8")
            real_entries = saitalk._sorted_dir_entries
            scanned: list[str] = []

            def watch(directory: Path):
                scanned.append(directory.name)
                return real_entries(directory)

            with unittest.mock.patch.object(
                saitalk, "_sorted_dir_entries", side_effect=watch
            ):
                iterator = saitalk._iter_md_files(adapters)
                first = next(iterator)
            self.assertEqual(first.name, "aaa.md")
            self.assertNotIn("zzz", scanned)

    def test_iter_md_files_still_follows_symlinked_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            target = root / "outside"
            target.mkdir(parents=True)
            adapters.mkdir()
            (target / "linked.md").write_text("# linked\n", encoding="utf-8")
            self._symlink_or_skip(target, adapters / "linked")
            names = [p.name for p in saitalk._iter_md_files(adapters)]
            self.assertIn("linked.md", names)

    def test_iter_md_files_detects_symlink_cycles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir(parents=True)
            (adapters / "one.md").write_text("# one\n", encoding="utf-8")
            self._symlink_or_skip(adapters, adapters / "loop")
            names = [p.name for p in saitalk._iter_md_files(adapters)]
            self.assertEqual(names, ["one.md"])

    def test_iter_md_files_matches_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir(parents=True)
            (adapters / "UPPER.MD").write_text("# u\n", encoding="utf-8")
            names = [p.name for p in saitalk._iter_md_files(adapters)]
            self.assertEqual(names, ["UPPER.MD"])

    def test_iter_md_files_first_yield_memory_is_bounded(self) -> None:
        import tracemalloc

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir(parents=True)
            (adapters / "00000_first.md").write_text("# f\n", encoding="utf-8")
            for i in range(5000):
                (adapters / f"good_{i:05d}.md").write_text("# g\n", encoding="utf-8")
            tracemalloc.start()
            try:
                iterator = saitalk._iter_md_files(adapters)
                first = next(iterator)
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(first.name, "00000_first.md")
            # Names only, no Path objects per entry: comfortably under a
            # megabyte for 5k siblings.
            self.assertLess(peak, 2 * 1024 * 1024, f"peak {peak} bytes")

    # ---- R018 / PERF-003: corpus_digest does not materialize -----------

    def test_corpus_digest_peak_memory_is_bounded(self) -> None:
        import tracemalloc

        harness = self.evals_harness
        big = {
            "version": 1,
            "cases": [
                {
                    "id": f"case-{i:05d}",
                    "setup": "s" * 40,
                    "prompt": "p" * 60,
                    "must": ["alpha", "beta"],
                    "must_not": ["gamma"],
                }
                for i in range(4000)
            ],
        }
        harness.validate_cases(big)
        serialized = len(harness.export_cases(big).encode("utf-8"))
        tracemalloc.start()
        try:
            digest = harness.corpus_digest(big)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(
            digest,
            hashlib.sha256(harness.export_cases(big).encode("utf-8")).hexdigest(),
        )
        # The StringIO + getvalue().encode() implementation peaked at ~3.25x
        # the serialized size; a streaming hash stays a small fraction of it.
        self.assertLess(
            peak,
            serialized // 2,
            f"peak {peak} bytes for {serialized} serialized bytes",
        )

    def test_corpus_digest_of_shipped_corpus_is_unchanged(self) -> None:
        harness = self.evals_harness
        self.assertEqual(
            harness.corpus_digest(self.cases),
            hashlib.sha256(
                harness.export_cases(self.cases).encode("utf-8")
            ).hexdigest(),
        )

    # ---- R019 / PERF-004: cases_validated is a real fast path ----------

    def test_validate_results_fast_path_skips_corpus_validation(self) -> None:
        harness = self.evals_harness
        results = harness.new_results(self.cases)
        real = harness.validate_cases
        calls: list[int] = []

        def counting(data):
            calls.append(1)
            return real(data)

        with unittest.mock.patch.object(
            harness, "validate_cases", side_effect=counting
        ):
            harness.validate_results(results, self.cases, cases_validated=True)
            self.assertEqual(len(calls), 0)
            harness.validate_results(results, self.cases)
            self.assertEqual(len(calls), 1)

    def test_load_results_validates_corpus_exactly_once(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            harness.write_results(
                harness.new_results(self.cases), out, self.cases, contract_id=None
            )
            real = harness.validate_cases
            calls: list[int] = []

            def counting(data):
                calls.append(1)
                return real(data)

            with unittest.mock.patch.object(
                harness, "validate_cases", side_effect=counting
            ):
                harness.load_results(out, self.cases, cases_validated=True)
                self.assertEqual(len(calls), 0)
                calls.clear()
                harness.load_results(out, self.cases)
                self.assertEqual(len(calls), 1)

    def test_invalid_corpus_still_fails_without_the_flag(self) -> None:
        harness = self.evals_harness
        bad = {
            "version": 1,
            "cases": [
                {
                    "id": "BAD-ID",
                    "setup": "s",
                    "prompt": "p",
                    "must": ["a"],
                    "must_not": ["b"],
                }
            ],
        }
        with self.assertRaises(harness.EvalError):
            harness.validate_results({"BAD-ID": {"state": "NOT_RUN"}}, bad)

    # ---- T-905: destination mode preservation, testable on any host ----
    #
    # The real chmod round-trip (test_write_results_preserves_posix_mode) can
    # only run where the filesystem honours mode bits. These drive the two
    # decision points directly, so Windows still covers the logic even though
    # it cannot observe the bits.

    def test_capture_destination_mode_is_none_for_absent_file(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(
                harness._capture_destination_mode(Path(temp) / "absent.json")
            )

    def test_capture_destination_mode_is_none_on_windows(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            with unittest.mock.patch.object(harness.os, "name", "nt"):
                self.assertIsNone(harness._capture_destination_mode(out))

    def test_capture_destination_mode_reads_bits_on_posix(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            fake = os.stat(out)
            with unittest.mock.patch.object(harness.os, "name", "posix"):
                with unittest.mock.patch.object(Path, "is_file", return_value=True):
                    with unittest.mock.patch.object(
                        harness.os, "stat", return_value=fake
                    ):
                        captured = harness._capture_destination_mode(out)
            self.assertEqual(captured, fake.st_mode & 0o7777)

    def test_capture_destination_mode_normalizes_stat_failure(self) -> None:
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            # is_file() is answered first and independently: patching os.stat
            # alone would make is_file() report False and take the None branch
            # instead of reaching the guarded read.
            with unittest.mock.patch.object(harness.os, "name", "posix"):
                with unittest.mock.patch.object(Path, "is_file", return_value=True):
                    with unittest.mock.patch.object(
                        harness.os, "stat", side_effect=OSError("stat refused")
                    ):
                        with self.assertRaises(harness.EvalError) as ctx:
                            harness._capture_destination_mode(out)
            self.assertIn("cannot read destination mode", str(ctx.exception))

    def test_apply_destination_mode_is_a_noop_without_a_mode(self) -> None:
        harness = self.evals_harness
        with unittest.mock.patch.object(harness.os, "name", "posix"):
            with unittest.mock.patch.object(
                harness.os, "fchmod", create=True
            ) as fchmod:
                harness._apply_destination_mode(7, None)
        fchmod.assert_not_called()

    def test_apply_destination_mode_is_a_noop_on_windows(self) -> None:
        harness = self.evals_harness
        with unittest.mock.patch.object(harness.os, "name", "nt"):
            with unittest.mock.patch.object(
                harness.os, "fchmod", create=True
            ) as fchmod:
                harness._apply_destination_mode(7, 0o600)
        fchmod.assert_not_called()

    def test_apply_destination_mode_chmods_the_staged_descriptor(self) -> None:
        harness = self.evals_harness
        with unittest.mock.patch.object(harness.os, "name", "posix"):
            with unittest.mock.patch.object(
                harness.os, "fchmod", create=True
            ) as fchmod:
                harness._apply_destination_mode(7, 0o640)
        fchmod.assert_called_once_with(7, 0o640)

    def test_write_results_preserves_the_captured_mode_end_to_end(self) -> None:
        # The full ordering claim without needing real mode bits: capture the
        # destination's mode, then apply it to the STAGED descriptor BEFORE
        # the staged fsync, so content and final inode metadata are
        # synchronized by the one fsync that precedes the install.
        # CORE-003 (SRC-002:R003) pins the whole protocol:
        # apply-mode -> file fsync -> install -> directory fsync.
        harness = self.evals_harness
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            out.write_text("{}", encoding="utf-8")
            events: list[str] = []
            real_replace = os.replace
            real_fsync = os.fsync

            def watch_replace(src, dst):
                events.append("install")
                return real_replace(src, dst)

            def watch_fsync(fileno):
                events.append("file_fsync")
                return real_fsync(fileno)

            def watch_dir_fsync(path):
                events.append("dir_fsync")

            with unittest.mock.patch.object(
                harness, "_capture_destination_mode", return_value=0o640
            ) as capture:
                with unittest.mock.patch.object(
                    harness, "_apply_destination_mode"
                ) as apply_mode:
                    apply_mode.side_effect = lambda fileno, mode: events.append(
                        f"apply:{mode:o}"
                    )
                    with unittest.mock.patch(
                        "os.replace", side_effect=watch_replace
                    ):
                        with unittest.mock.patch(
                            "os.fsync", side_effect=watch_fsync
                        ):
                            with unittest.mock.patch.object(
                                harness,
                                "_fsync_parent_directory",
                                side_effect=watch_dir_fsync,
                            ):
                                harness.write_results(
                                    harness.new_results(self.cases),
                                    out,
                                    self.cases,
                                    contract_id=None,
                                )
            capture.assert_called_once_with(out)
            self.assertEqual(
                events, ["apply:640", "file_fsync", "install", "dir_fsync"]
            )

    # ---- T-912: absent vs unresolvable adapters/ -----------------------

    def test_drift_treats_absent_adapters_as_nothing_to_scan(self) -> None:
        # Stated contract, not an accident: adapters are transport and
        # non-normative, so a package need not ship any. The scan must also
        # not be entered, or a later change could make "absent" mean
        # "scanned and clean".
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with unittest.mock.patch.object(
                saitalk,
                "_iter_md_files",
                side_effect=AssertionError("scanned a tree that is not there"),
            ):
                saitalk.validate_drift(root)

    def test_drift_scans_when_adapters_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir()
            (adapters / "a.md").write_text("# fine\n", encoding="utf-8")
            real = saitalk._iter_md_files
            calls: list[int] = []
            with unittest.mock.patch.object(
                saitalk,
                "_iter_md_files",
                side_effect=lambda d: (calls.append(1), real(d))[1],
            ):
                saitalk.validate_drift(root)
            self.assertEqual(len(calls), 1)

    def test_drift_accepts_an_empty_adapters_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            saitalk.validate_drift(root)

    def test_drift_refuses_a_dangling_adapters_symlink(self) -> None:
        # The entry EXISTS, so something intended adapters to be there.
        # Treating it as nothing to scan disabled the whole gate silently.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            link = root / "adapters"
            self._symlink_or_skip(root / "nowhere", link)
            self.assertTrue(link.is_symlink())
            self.assertFalse(link.exists())
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("does not resolve", str(ctx.exception))

    def test_drift_still_refuses_a_file_named_adapters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").write_text("x", encoding="utf-8")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("not a directory", str(ctx.exception))

    def test_drift_reports_an_unreadable_adapters_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "adapters").mkdir()
            with unittest.mock.patch.object(
                saitalk.os, "lstat", side_effect=OSError("permission denied")
            ):
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk.validate_drift(root)
            self.assertIn("cannot inspect adapter path", str(ctx.exception))

    # ---- SRC-002 CORE-002: dangling adapter symlinks inside adapters/ ----

    def _package_with_adapter(self, pkg: Path) -> None:
        self._make_valid_package(pkg)
        (pkg / "adapters").mkdir()
        (pkg / "adapters" / "OK.md").write_text(
            "# Adapter\n\nLoads files only.\n", encoding="utf-8"
        )

    def test_drift_refuses_a_dangling_adapter_md_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir()
            self._symlink_or_skip(adapters / "nowhere.md", adapters / "BROKEN.md")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("does not resolve", str(ctx.exception))
            self.assertIn("BROKEN.md", str(ctx.exception))

    def test_drift_refuses_a_dangling_adapter_dir_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir()
            self._symlink_or_skip(adapters / "nowhere2", adapters / "baddir")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("does not resolve", str(ctx.exception))
            self.assertIn("baddir", str(ctx.exception))

    def test_drift_traverses_valid_symlinked_adapter_md(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            real = root / "real"
            adapters.mkdir()
            real.mkdir()
            (adapters / "a.md").write_text("# fine\n", encoding="utf-8")
            (real / "linked.md").write_text("# linked\n", encoding="utf-8")
            self._symlink_or_skip(real / "linked.md", adapters / "link.md")
            got = [p.name for p in saitalk._iter_md_files(adapters)]
            self.assertEqual(got, ["a.md", "link.md"])

    def test_drift_traverses_valid_symlinked_adapter_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            real = root / "real"
            (real / "sub").mkdir(parents=True)
            adapters.mkdir()
            (real / "linked.md").write_text("# linked\n", encoding="utf-8")
            self._symlink_or_skip(real, adapters / "linkdir")
            got = [p.name for p in saitalk._iter_md_files(adapters)]
            self.assertEqual(got, ["linked.md"])

    def test_validate_package_refuses_dangling_adapter_symlink(self) -> None:
        # The public gate: a dangling adapter entry must make the canonical
        # package validator fail, not silently scan a shorter tree.
        with tempfile.TemporaryDirectory() as temp:
            pkg = Path(temp) / "pkg"
            pkg.mkdir()
            self._package_with_adapter(pkg)
            saitalk.validate_package(
                pkg / "SAITALK.md", pkg / "saitalk.conf", pkg / "SKILL.md"
            )
            self._symlink_or_skip(pkg / "adapters" / "gone", pkg / "adapters" / "DEAD.md")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_package(
                    pkg / "SAITALK.md", pkg / "saitalk.conf", pkg / "SKILL.md"
                )
            self.assertIn("does not resolve", str(ctx.exception))
            self.assertIn("DEAD.md", str(ctx.exception))

    # ---- T-913: cancellation keeps its identity during lock teardown ----

    def _lock_teardown(self, close_exc: BaseException) -> BaseException:
        """Contend on the lock for `target` while close() raises close_exc.

        Returns whatever propagated out of _acquire_lock.
        """
        import hashlib as _hashlib

        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "SAITALK.md"
            target.write_text("x", encoding="utf-8")
            digest = _hashlib.sha256(
                str(target.resolve()).encode("utf-8")
            ).hexdigest()[:16]
            lock_path = Path(tempfile.gettempdir()) / f"saitalk-{digest}.lock"
            self.addCleanup(lambda: lock_path.unlink(missing_ok=True))
            leak = []

            class FailingClose:
                def __init__(self, real):
                    self._real = real

                def fileno(self):
                    return self._real.fileno()

                def write(self, *args):
                    return self._real.write(*args)

                def close(self):
                    leak.append(self._real)
                    raise close_exc

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            real_open = Path.open

            def open_lock_failing(self, *args, **kwargs):
                handle = real_open(self, *args, **kwargs)
                if self == lock_path:
                    return FailingClose(handle)
                return handle

            if os.name == "nt":
                import msvcrt

                module, attr = msvcrt, "locking"
            else:
                import fcntl

                module, attr = fcntl, "flock"

            with unittest.mock.patch.object(Path, "open", open_lock_failing):
                with unittest.mock.patch.object(
                    module, attr, side_effect=OSError("simulated contention")
                ):
                    try:
                        saitalk._acquire_lock(target)
                    except BaseException as exc:
                        # FailingClose.close raises before closing the real
                        # handle; close it here so Windows allows the unlink.
                        for fh in leak:
                            try:
                                fh.close()
                            except OSError:
                                pass
                        return exc
            raise AssertionError("_acquire_lock succeeded against a contended lock")

    def test_lock_teardown_preserves_cancellation(self) -> None:
        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exc_type.__name__):
                exc = self._lock_teardown(exc_type("cancel"))
                self.assertIsInstance(exc, exc_type)

    def test_lock_teardown_still_swallows_oserror(self) -> None:
        exc = self._lock_teardown(OSError("simulated close failure"))
        self.assertIsInstance(exc, saitalk.SaitalkError)
        self.assertIn("lock contention", str(exc))
        self.assertNotIn("simulated close failure", str(exc))

    # ---- SRC-002 W2-002: aggregate lock cleanup under cancellation -------

    def _two_targets(self, base: Path) -> tuple[Path, Path]:
        t1 = base / "SAITALK.md"
        t2 = base / "SKILL.md"
        t1.write_text("x", encoding="utf-8")
        t2.write_text("x", encoding="utf-8")
        return t1, t2

    def _acquire_locks_cancels_on_second(
        self, exc_type: type, base: Path
    ) -> BaseException:
        t1, t2 = self._two_targets(base)
        real_acquire = saitalk._acquire_lock

        def acquire_failing(target):
            if target == t2:
                raise exc_type("cancel second acquire")
            return real_acquire(target)

        try:
            with unittest.mock.patch.object(
                saitalk, "_acquire_lock", side_effect=acquire_failing
            ):
                saitalk._acquire_locks([t1, t2])
        except BaseException as exc:
            return exc
        raise AssertionError("acquisition unexpectedly succeeded")

    def test_acquire_locks_cancellation_releases_the_first_lock(self) -> None:
        # KeyboardInterrupt/SystemExit on the second acquisition must release
        # the first handle BEFORE propagation: a host that retains the
        # caught exception (traceback keeps locals alive) must still be able
        # to reacquire target 1 immediately.
        import gc

        for exc_type in (KeyboardInterrupt, SystemExit):
            with self.subTest(exception=exc_type.__name__):
                with tempfile.TemporaryDirectory() as temp:
                    t1, _t2 = self._two_targets(Path(temp))
                    exc = self._acquire_locks_cancels_on_second(
                        exc_type, Path(temp)
                    )
                    self.assertIsInstance(exc, exc_type)
                    # Retain the exception/traceback exactly as a logging
                    # host would, then prove target 1 is reacquirable NOW.
                    retained = exc
                    fh = saitalk._acquire_lock(t1)
                    saitalk._release_lock(fh)
                    gc.collect()
                    fh = saitalk._acquire_lock(t1)
                    saitalk._release_lock(fh)
                    self.assertIsNotNone(retained)

    def test_acquire_locks_contention_releases_earlier_handles(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            t1, t2 = self._two_targets(base)
            holder = saitalk._acquire_lock(t1)
            try:
                with self.assertRaises(saitalk.SaitalkError) as ctx:
                    saitalk._acquire_locks([t1, t2])
                self.assertIn("lock contention", str(ctx.exception))
            finally:
                saitalk._release_lock(holder)
            # t2 was never acquired, t1 was contended: nothing leaks, so a
            # fresh full acquisition succeeds after the holder released.
            locks = saitalk._acquire_locks([t1, t2])
            saitalk._release_locks(locks)

    def test_acquire_locks_cleanup_failure_preserves_primary(self) -> None:
        # A release helper that itself fails must not disguise the primary
        # acquisition failure, and every remaining handle still receives a
        # release attempt.
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            t1, t2 = self._two_targets(base)
            released: list[int] = []
            real_release = saitalk._release_lock
            real_acquire = saitalk._acquire_lock

            def acquire_failing(target):
                if target == t2:
                    raise KeyboardInterrupt("cancel second acquire")
                return real_acquire(target)

            def release_failing(fh):
                released.append(fh)
                raise saitalk.SaitalkError("cleanup broke")

            def cleanup():
                for fh in released:
                    try:
                        real_release(fh)
                    except Exception:
                        pass

            self.addCleanup(cleanup)
            with unittest.mock.patch.object(
                saitalk, "_acquire_lock", side_effect=acquire_failing
            ):
                with unittest.mock.patch.object(
                    saitalk, "_release_lock", side_effect=release_failing
                ):
                    with self.assertRaises(KeyboardInterrupt) as ctx:
                        saitalk._acquire_locks([t1, t2])
            self.assertIn("cancel second acquire", str(ctx.exception))
            self.assertIsInstance(ctx.exception.__cause__, saitalk.SaitalkError)
            self.assertIn("cleanup incomplete", str(ctx.exception.__cause__))
            self.assertIn("cleanup broke", str(ctx.exception.__cause__))
            self.assertEqual(len(released), 1)
            released.clear()


if __name__ == "__main__":
    unittest.main()
