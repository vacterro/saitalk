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
    return subprocess.run(
        [sys.executable, "-m", "scripts.saitalk", *args],
        capture_output=True,
        text=True,
        cwd=cwd or REPO,
        timeout=60,
        check=False,
    )


def run_evals_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "evals.harness", *args],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
        check=False,
    )


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
        original = saitalk._write_temp
        calls = {"n": 0}

        def wrapper(target, content):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise saitalk.SaitalkError("simulated write failure")
            return original(target, content)

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
        self._assert_rollback("scripts.saitalk._write_temp", self._fail_write(1))

    def test_rollback_second_write(self) -> None:
        self._assert_rollback("scripts.saitalk._write_temp", self._fail_write(2))

    def test_rollback_replacement(self) -> None:
        self._assert_rollback("scripts.saitalk._replace_temp", self._fail_replace(2))

    def test_success_after_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch(
                "scripts.saitalk._write_temp", side_effect=self._fail_write(1)
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

            def fail_restore_contract(target, original):
                restore_attempts.append(target)
                if target == contract:
                    raise saitalk.SaitalkError("simulated restore failure")
                return original_restore(target, original)

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
            with unittest.mock.patch(
                "scripts.saitalk._restore_bytes", wraps=saitalk._restore_bytes
            ) as restore_spy, unittest.mock.patch(
                "scripts.saitalk._write_temp", side_effect=self._fail_write(1)
            ), self.assertRaises(saitalk.SaitalkError):
                saitalk.refresh(contract, conf, skill)
            restore_spy.assert_not_called()

    def test_zero_replacements_means_zero_restore_calls_second_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            with unittest.mock.patch(
                "scripts.saitalk._restore_bytes", wraps=saitalk._restore_bytes
            ) as restore_spy, unittest.mock.patch(
                "scripts.saitalk._write_temp", side_effect=self._fail_write(2)
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
        proc = run_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("SAITALK PASS", proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_validate_stale_fails_exit_one_no_traceback(self) -> None:
        proc = run_cli(
            "validate",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_print_id_stale_fails_exit_one_no_traceback(self) -> None:
        proc = run_cli(
            "print-id",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("SAITALK FAIL", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_refresh_then_print_id(self) -> None:
        refresh = run_cli(
            "refresh",
            "--contract", str(self.contract),
            "--config", str(self.conf),
            "--skill", str(self.skill),
        )
        self.assertEqual(refresh.returncode, 0)
        self.assertIn("SAITALK REFRESHED:", refresh.stdout)
        contract_id = refresh.stdout.strip().split("contract_id=")[1]
        printed = run_cli(
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
        proc = run_cli(
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
        proc = run_cli(
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

    def test_custom_package_clean_passes(self) -> None:
        pkg = self.root / "pkg"
        pkg.mkdir()
        contract, conf, skill = make_root(pkg)
        saitalk.refresh(contract, conf, skill)
        (pkg / "adapters").mkdir()
        (pkg / "adapters" / "OK.md").write_text(
            "# Adapter\n\nLoads files only.\n", encoding="utf-8"
        )
        proc = run_cli(
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
        proc = run_cli(
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
        proc = run_cli(
            "validate",
            "--contract", str(contract),
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("coherent package root", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)


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
            self.assertIn("completion first", str(ctx.exception))

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
        proc = run_cli(
            "validate",
            "--state", str(REPO / "examples" / "STATE.md"),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_package_validates(self) -> None:
        proc = run_cli("validate")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_version_sources_agree(self) -> None:
        version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$")
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Current release: {version}.", readme)
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
            proc = run_evals_cli("validate", "--cases", str(bad))
            self.assertEqual(proc.returncode, 1)
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
            proc = run_evals_cli("validate-results", "--results", str(out))
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
            proc = run_evals_cli("validate-results", "--results", str(out))
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
        proc = run_evals_cli("export")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, self.evals_harness.export_cases(data))

    def test_export_command_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "export.json"
            proc = run_evals_cli("export", "--out", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS EXPORT", proc.stdout)
            data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
            self.assertEqual(
                out.read_text(encoding="utf-8"), self.evals_harness.export_cases(data)
            )

    def test_validate_results_command_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            init = run_evals_cli("init-results", "--out", str(out))
            self.assertEqual(init.returncode, 0, init.stderr)
            proc = run_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("EVALS RESULTS VALID", proc.stdout)

    def test_validate_results_command_rejects_foreign_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            run_evals_cli("init-results", "--out", str(out))
            written = json.loads(out.read_text(encoding="utf-8"))
            written["results"]["not-a-case"] = {"state": "PASS"}
            out.write_text(json.dumps(written), encoding="utf-8")
            proc = run_evals_cli("validate-results", "--results", str(out))
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
            proc = run_evals_cli("validate-results", "--results", str(out))
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
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertNotIn("EVALS RESULTS CURRENT", proc.stdout)
            self.assertNotIn("Traceback", proc.stdout)
            self.assertNotIn("Traceback", proc.stderr)
            self.assertIn("could not be validated", proc.stdout)

    def test_validate_results_command_rejects_foreign_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp) / "results.json"
            run_evals_cli("init-results", "--out", str(out))
            written = json.loads(out.read_text(encoding="utf-8"))
            written["corpus"]["digest"] = "0" * 64
            out.write_text(json.dumps(written), encoding="utf-8")
            proc = run_evals_cli("validate-results", "--results", str(out))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("different eval corpus", proc.stderr)


if __name__ == "__main__":
    unittest.main()
