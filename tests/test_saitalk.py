from __future__ import annotations

import sys
import tempfile
import unittest
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
                f"saitalk_contract: {expected}\nsaitalk_status: active\n",
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
                "saitalk_status: inactive\n",
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
                "saitalk_status: active\n",
                encoding="utf-8",
            )
            saitalk.validate_state(state, "saitalk-12345678")

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
