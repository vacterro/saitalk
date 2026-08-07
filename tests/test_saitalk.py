from __future__ import annotations

import json
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


def make_root(root: Path) -> tuple[Path, Path, Path]:
    contract = root / "SAITALK.md"
    conf = root / "saitalk.conf"
    skill = root / "SKILL.md"
    contract.write_text(
        "# Contract\n\ncontract_id: saitalk-00000000\n\nBody.\n", encoding="utf-8"
    )
    conf.write_text(CONF_FMT.format(reply="en", style="caveman-ded"), encoding="utf-8")
    skill.write_text("# SKILL\n\nLoad order mechanics only.\n", encoding="utf-8")
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
        self.contract.write_bytes(self.contract.read_bytes().replace(b"\n", b"\r\n"))
        self.conf.write_bytes(self.conf.read_bytes().replace(b"\n", b"\r\n"))
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


class SealTests(unittest.TestCase):
    def test_skill_mutation_invalidates_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = make_root(root)
            expected = saitalk.refresh(contract, conf, skill)
            skill.write_text("# SKILL\n\nMUTATED normative line.\n", encoding="utf-8")
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
        self.assertIn(f"## {version} - ", changelog)


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

    def test_export_is_deterministic(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        first = self.evals_harness.export_cases(data)
        second = self.evals_harness.export_cases(data)
        self.assertEqual(first, second)

    def test_results_states(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        self.assertEqual(set(results.values()), {"NOT_RUN"})
        for state in results.values():
            self.assertIn(state, self.evals_harness.EVAL_STATES)

    def test_invalid_result_state_fails(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = "MAYBE"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(self.evals_harness.EvalError) as ctx:
                self.evals_harness.write_results(results, Path(temp) / "results.json")
            self.assertIn("invalid result state", str(ctx.exception))

    def test_write_results_roundtrip(self) -> None:
        data = self.evals_harness.load_cases(REPO / "evals" / "cases.json")
        results = self.evals_harness.new_results(data)
        results[data["cases"][0]["id"]] = "PASS"
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "results.json"
            self.evals_harness.write_results(results, path)
            written = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(written["results"], dict(sorted(results.items())))


if __name__ == "__main__":
    unittest.main()
