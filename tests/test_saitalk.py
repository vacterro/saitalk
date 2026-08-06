from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "saitalk.py"
SPEC = importlib.util.spec_from_file_location("saitalk", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class SaitalkTests(unittest.TestCase):
    def create_files(self, root: Path) -> tuple[Path, Path]:
        contract = root / "SAITALK.md"
        conf = root / "saitalk.conf"

        contract.write_text(
            "# Contract\n\ncontract_id: saitalk-00000000\n\nBody.\n",
            encoding="utf-8",
        )
        conf.write_text(
            "spec_version=1\n"
            "reply_language=en\n"
            "chat_style=caveman-ded-en\n"
            "artifact_language=en\n"
            "review_mode=evidence-gated\n"
            "response_budget=5\n"
            "contract_id=saitalk-00000000\n",
            encoding="utf-8",
        )
        return contract, conf

    def test_refresh_validate_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf = self.create_files(root)

            expected = module.refresh(contract, conf)
            actual, config = module.validate(contract, conf)

            self.assertEqual(actual, expected)
            self.assertEqual(config["reply_language"], "en")

            state = root / "STATE.md"
            state.write_text(
                f"saitalk_contract: {expected}\nsaitalk_status: active\n",
                encoding="utf-8",
            )
            module.validate_state(state, expected)

    def test_invalid_language_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            contract, conf = self.create_files(root)
            text = conf.read_text(encoding="utf-8").replace(
                "reply_language=en", "reply_language=eesti"
            )
            conf.write_text(text, encoding="utf-8")
            with self.assertRaises(module.SaitalkError):
                module.validate(contract, conf)

    def test_stale_state_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-deadbeef\n",
                encoding="utf-8",
            )
            with self.assertRaises(module.SaitalkError):
                module.validate_state(state, "saitalk-12345678")

    def test_inactive_status_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: inactive\n",
                encoding="utf-8",
            )
            with self.assertRaises(module.SaitalkError):
                module.validate_state(state, "saitalk-12345678")

    def test_active_status_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state = root / "STATE.md"
            state.write_text(
                "saitalk_contract: saitalk-12345678\n"
                "saitalk_status: active\n",
                encoding="utf-8",
            )
            module.validate_state(state, "saitalk-12345678")


if __name__ == "__main__":
    unittest.main()
