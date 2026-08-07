from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import saitalk  # noqa: E402


class SaitalkTests(unittest.TestCase):
    def create_files(self, root: Path) -> tuple[Path, Path, Path]:
        contract = root / "SAITALK.md"
        conf = root / "saitalk.conf"
        skill = root / "SKILL.md"

        contract.write_text(
            "# Contract\n\ncontract_id: saitalk-00000000\n\nBody.\n",
            encoding="utf-8",
        )
        conf.write_text(
            "spec_version=2\n"
            "reply_language=en\n"
            "chat_style=caveman-ded\n"
            "artifact_language=en\n"
            "review_mode=evidence-gated\n"
            "response_budget=5\n"
            "contract_id=saitalk-00000000\n",
            encoding="utf-8",
        )
        skill.write_text(
            "# SKILL\n\nLoad order mechanics only.\n",
            encoding="utf-8",
        )
        return contract, conf, skill

    def test_refresh_validate_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)

            expected = saitalk.refresh(contract, conf, skill)
            actual, config = saitalk.validate(contract, conf, skill)

            self.assertEqual(actual, expected)
            self.assertEqual(config["reply_language"], "en")

            state = root / "STATE.md"
            state.write_text(
                f"saitalk_contract: {expected}\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, expected)

    def test_invalid_language_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            text = conf.read_text(encoding="utf-8").replace(
                "reply_language=en", "reply_language=eesti"
            )
            conf.write_text(text, encoding="utf-8")
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate(contract, conf, skill)

    def test_stale_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-deadbeef\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678")

    def test_inactive_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: inactive\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate_state(state, "saitalk-12345678")

    def test_active_status_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678")

    def _state(self, root: Path, contract: str, status: str, voice: str) -> Path:
        state = root / "STATE.md"
        state.write_text(
            f"saitalk_contract: {contract}\n"
            f"saitalk_status: {status}\n"
            f"saitalk_voice: {voice}\n",
            encoding="utf-8",
        )
        return state

    def test_duplicate_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: active\n"
                "saitalk_status: active\n"
                "saitalk_voice: active\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_state(state, "saitalk-12345678")
            self.assertIn("exactly one match", str(ctx.exception))

    def test_missing_voice_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: active\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_state(state, "saitalk-12345678")
            self.assertIn("exactly one match", str(ctx.exception))

    def test_invalid_voice_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678", "active", "partial")
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_state(state, "saitalk-12345678")
            self.assertIn("expected 'active' or 'suspended'", str(ctx.exception))

    def test_suspended_voice_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = self._state(root, "saitalk-12345678", "active", "suspended")
            saitalk.validate_state(state, "saitalk-12345678")

    def test_voice_command_standalone(self) -> None:
        self.assertEqual(saitalk.voice_command("stop caveman"), "suspended")
        self.assertEqual(saitalk.voice_command("normal mode"), "suspended")
        self.assertEqual(saitalk.voice_command("resume caveman"), "active")
        self.assertEqual(saitalk.voice_command("saitalk mode"), "active")

    def test_voice_command_embedded_does_not_fire(self) -> None:
        self.assertIsNone(saitalk.voice_command("say \"stop caveman\" now"))
        self.assertIsNone(saitalk.voice_command("```normal mode```"))
        self.assertIsNone(saitalk.voice_command("she typed stop caveman"))
        self.assertIsNone(saitalk.voice_command("does the phrase normal mode do anything?"))
        self.assertIsNone(saitalk.voice_command("stop caveman please"))
        self.assertIsNone(saitalk.voice_command("resume caveman now"))

    def test_artifact_language_fixed_precedence(self) -> None:
        r = saitalk.resolve_artifact_language
        self.assertEqual(r("en"), "en")
        self.assertEqual(r("en", task_language="ru"), "ru")
        self.assertEqual(r("en", existing_language="et"), "et")
        self.assertEqual(r("en", repo_language="ru"), "ru")
        self.assertEqual(r("en", task_language="ru", existing_language="et"), "ru")
        self.assertEqual(r("en", existing_language="et", repo_language="ru"), "et")
        self.assertEqual(
            r("en", task_language="ru", existing_language="et", repo_language="ru"),
            "ru",
        )

    def test_artifact_language_auto_cascade(self) -> None:
        r = saitalk.resolve_artifact_language
        self.assertEqual(r("auto"), "en")
        self.assertEqual(r("auto", existing_language="et"), "et")
        self.assertEqual(r("auto", existing_language="et", task_language="ru"), "et")
        self.assertEqual(r("auto", task_language="ru"), "ru")
        self.assertEqual(r("auto", repo_language="ru"), "ru")
        self.assertEqual(r("auto", task_language="ru", repo_language="et"), "ru")

    def test_artifact_language_ignores_non_languages(self) -> None:
        r = saitalk.resolve_artifact_language
        self.assertEqual(r("auto", existing_language="de"), "en")
        self.assertEqual(r("en", task_language="de"), "en")
        self.assertEqual(r("ru", existing_language=None, repo_language=None), "ru")

    def _bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def test_refresh_rejects_identical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
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

    def test_refresh_rolls_back_on_first_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            with unittest.mock.patch("test_saitalk.saitalk._write_temp",
                                     side_effect=self._fail_write(1)):
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.refresh(contract, conf, skill)
            self.assertEqual(self._bytes(contract), before_c)
            self.assertEqual(self._bytes(conf), before_f)
            self.assertFalse((root / "SAITALK.md.tmp").exists())
            self.assertFalse((root / "saitalk.conf.tmp").exists())

    def test_refresh_rolls_back_on_second_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            with unittest.mock.patch("test_saitalk.saitalk._write_temp",
                                     side_effect=self._fail_write(2)):
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.refresh(contract, conf, skill)
            self.assertEqual(self._bytes(contract), before_c)
            self.assertEqual(self._bytes(conf), before_f)
            self.assertFalse((root / "SAITALK.md.tmp").exists())
            self.assertFalse((root / "saitalk.conf.tmp").exists())

    def test_refresh_rolls_back_on_replacement_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            before_c = self._bytes(contract)
            before_f = self._bytes(conf)
            with unittest.mock.patch("test_saitalk.saitalk._replace_temp",
                                     side_effect=self._fail_replace(2)):
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.refresh(contract, conf, skill)
            self.assertEqual(self._bytes(contract), before_c)
            self.assertEqual(self._bytes(conf), before_f)
            self.assertFalse((root / "SAITALK.md.tmp").exists())
            self.assertFalse((root / "saitalk.conf.tmp").exists())

    def test_refresh_success_after_rollback_leaves_valid_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            with unittest.mock.patch("test_saitalk.saitalk._write_temp",
                                     side_effect=self._fail_write(1)):
                with self.assertRaises(saitalk.SaitalkError):
                    saitalk.refresh(contract, conf, skill)
            expected = saitalk.refresh(contract, conf, skill)
            actual, config = saitalk.validate(contract, conf, skill)
            self.assertEqual(actual, expected)
            self.assertEqual(config["reply_language"], "en")

    def test_concurrent_refresh_lock_contention(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            holder = saitalk._acquire_lock(contract)
            try:
                code = (
                    "import sys; sys.path.insert(0, %r); import saitalk\n"
                    "from pathlib import Path\n"
                    "try:\n"
                    "    saitalk.refresh(Path(%r), Path(%r), Path(%r))\n"
                    "    print('NO-LOCK'); sys.exit(1)\n"
                    "except saitalk.SaitalkError as exc:\n"
                    "    print('LOCK:' + str(exc)); sys.exit(0)\n"
                ) % (
                    str(SCRIPTS),
                    str(contract),
                    str(conf),
                    str(skill),
                )
                proc = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            finally:
                saitalk._release_lock(holder)
            self.assertIn("LOCK:", proc.stdout)
            self.assertIn("lock contention", proc.stdout)
            self.assertEqual(proc.returncode, 0)

    def _lang_conf(self, language: str) -> str:
        return (
            "spec_version=2\n"
            f"reply_language={language}\n"
            "chat_style=caveman-ded\n"
            "artifact_language=en\n"
            "review_mode=evidence-gated\n"
            "response_budget=5\n"
            "contract_id=saitalk-00000000\n"
        )

    def test_every_allowed_language_with_caveman_ded_passes(self) -> None:
        for language in ("en", "et", "ru", "auto"):
            with self.subTest(language=language):
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp)
                    contract, conf, skill = self.create_files(root)
                    conf.write_text(self._lang_conf(language), encoding="utf-8")
                    saitalk.refresh(contract, conf, skill)
                    _, config = saitalk.validate(contract, conf, skill)
                    self.assertEqual(config["reply_language"], language)
                    self.assertEqual(config["chat_style"], "caveman-ded")

    def test_legacy_caveman_ded_en_fails_with_exact_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            conf.write_text(
                self._lang_conf("ru").replace("caveman-ded\n", "caveman-ded-en\n"),
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, skill)
            message = str(ctx.exception)
            self.assertIn("legacy value 'caveman-ded-en'", message)
            self.assertIn("chat_style=caveman-ded", message)
            self.assertIn("reply_language=en|et|ru|auto", message)

    def test_unknown_style_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            conf.write_text(
                self._lang_conf("en").replace("caveman-ded\n", "polite-robot\n"),
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError):
                saitalk.validate(contract, conf, skill)

    def test_clean_transport_passes_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir()
            (adapters / "BOOTSTRAP.md").write_text(
                "# Bootstrap\n\nLoad SAITALK.md and saitalk.conf.\n",
                encoding="utf-8",
            )
            saitalk.validate_drift(root)

    def test_contradictory_transport_fails_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapters = root / "adapters"
            adapters.mkdir()
            (adapters / "BOOTSTRAP.md").write_text(
                "# Bootstrap\n\nHost rules have higher priority than SAITALK.\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate_drift(root)
            self.assertIn("drift:", str(ctx.exception))
            self.assertIn("higher priority", str(ctx.exception))

    def test_digest_has_sixteen_hex(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            expected = saitalk.refresh(contract, conf, skill)
            self.assertTrue(expected.startswith("saitalk-"))
            self.assertEqual(len(expected), len("saitalk-") + 16)

    def test_skill_mutation_invalidates_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            expected = saitalk.refresh(contract, conf, skill)
            skill.write_text(
                "# SKILL\n\nLoad order mechanics only, with a mutated normative line.\n",
                encoding="utf-8",
            )
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, skill)
            self.assertIn("stale contract_id", str(ctx.exception))

    def test_missing_manifest_member_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            skill.unlink()
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, skill)
            self.assertIn("missing file", str(ctx.exception))

    def test_manifest_member_path_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf, skill = self.create_files(root)
            with self.assertRaises(saitalk.SaitalkError) as ctx:
                saitalk.validate(contract, conf, root / "OTHER.md")
            self.assertIn("manifest member mismatch", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
