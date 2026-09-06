from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from spotguard.cli import _parse_callback
from spotguard.config import load_settings
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.telegram import candidate_message, proposal_message

from tests.helpers import config_dict, write_config


class TelegramAndCallbackTests(unittest.TestCase):
    def test_agent_os_seeded_demo_is_paper_only_and_price_aligned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            service = SpotGuard(settings)
            result = service.create_demo_candidate("BTCUSDT", Decimal("60000"), notify=True, dry_run=True)
            self.assertTrue(result["created"])
            self.assertAlmostEqual(result["candidate"]["price"], 60000.0, places=4)
            self.assertTrue(result["candidate"]["metrics"]["paper_demo"])
            self.assertEqual(result["notification"]["transport"], "dry-run")

    def test_candidate_and_proposal_callbacks_fit_telegram_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            service = SpotGuard(settings)
            candidate = service.scan(symbols=["BTCUSDT"], synthetic=True)["results"][0]["candidate"]
            _, candidate_buttons = candidate_message(candidate)
            self.assertLessEqual(len(candidate_buttons[0]["value"].encode()), 64)
            result = service.create_proposal(
                candidate["id"],
                Decimal(str(candidate["price"])) * Decimal("0.999"),
                Decimal(str(candidate["price"])),
                Decimal("6"),
                "Paper review passed.",
            )
            proposal = result["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            code = service.signer.paper_confirmation_code(proposal["canonical_json"])
            _, proposal_buttons = proposal_message(proposal, token, code)
            for button in proposal_buttons:
                self.assertLessEqual(len(("tgcmd:" + button["command"]).encode()), 64)
            self.assertEqual(proposal_buttons[0]["command"],
                             f"/binance_spotguard paper-approve {proposal['id']} {code}")

    def test_malformed_callback_is_rejected(self) -> None:
        with self.assertRaises(SecurityError):
            _parse_callback("sg:approve:p-deadbeef0000:not-a-valid-token;rm -rf")

    def test_openclaw_presentation_delivery_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_openclaw = root / "fake-openclaw"
            fake_openclaw.write_text(
                "#!/bin/sh\n"
                "if [ \"${3:-}\" = \"--help\" ]; then echo --presentation; exit 0; fi\n"
                "echo '{\"sent\":true}'\n",
                encoding="utf-8",
            )
            fake_openclaw.chmod(0o700)
            raw = config_dict(root)
            raw["telegram"]["enabled"] = True
            raw["openclaw"]["command"] = str(fake_openclaw)
            config = root / "config.json"
            config.write_text(json.dumps(raw), encoding="utf-8")
            service = SpotGuard(load_settings(config))
            result = service.create_demo_candidate("BTCUSDT", Decimal("60000"), notify=True)
            self.assertTrue(result["notification"]["delivered"])
            self.assertEqual(result["notification"]["transport"], "presentation")

    def test_paper_close_controls_are_exact_native_commands(self) -> None:
        from spotguard.telegram import paper_close_message
        position = {"symbol": "BTCUSDT", "economic_position_id": "pe-123", "requested_percentage": "50",
                    "actual_executed_percentage": "50",
                    "aggregate_quantity": "1", "close_quantity": "0.5", "remaining_quantity": "0.5",
                    "average_cost": "100", "reference_bid": "100", "estimated_gross_proceeds": "50",
                    "estimated_fee_usdt": "0.05", "estimated_fee_asset": "USDT", "estimated_net_proceeds": "49.95",
                    "estimated_realized_pnl": "-0.05", "position_id": "pp-123"}
        close = {"id": "pc-123", "expires_at": "2999-01-01T00:00:00Z"}
        _, buttons = paper_close_message(position, close, "unused-token", "CODE1234")
        self.assertEqual(buttons[0]["command"], "/binance_spotguard close-approve pp-123 CODE1234")
        self.assertEqual(buttons[1]["command"], "/binance_spotguard close-reject pp-123 CODE1234")
        self.assertTrue(all("value" not in item for item in buttons))


if __name__ == "__main__":
    unittest.main()
