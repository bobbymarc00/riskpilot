from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import _parse_callback
from spotguard.config import load_settings
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.policy import PolicyError
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.telegram import OpenClawMessenger, proposal_message
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config


SNAPSHOT = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
    Decimal("5"), Decimal("0.001"), "TRADING", 1)


class DurableApprovalTests(unittest.TestCase):
    def make(self, root: Path):
        settings = load_settings(write_config(root))
        service = SpotGuard(settings)
        proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
        code = service.signer.paper_confirmation_code(proposal["canonical_json"])
        token = service.signer.approval_token(proposal["canonical_json"])
        return settings, service, proposal, code, token

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_callback_survives_creator_exit_and_router_restart(self, spot, klines):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings, creator, proposal, code, token = self.make(root)
            data = f"sg:approve:{proposal['id']}:{token}"
            del creator
            restarted = SpotGuard(settings)
            parsed = _parse_callback(data)
            claim = restarted.claim(parsed["proposal_id"], parsed["token"],
                settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            result = restarted.execute_paper(proposal["id"], claim["lease"])
            self.assertEqual(result["status"], "EXECUTED")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_presentation_uses_supported_command_actions_and_fallback(self, spot, klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, code, token = self.make(Path(directory))
            message, buttons = proposal_message(proposal, token, code)
            presentation = OpenClawMessenger._presentation(buttons)
            rendered = presentation["blocks"][0]["buttons"][0]
            self.assertEqual(rendered["action"]["type"], "command")
            self.assertEqual(rendered["action"]["command"],
                             f"/binance_spotguard paper-approve {proposal['id']} {code}")
            self.assertIn(f"/spot paper-approve {proposal['id']} {code}", message)
            self.assertIn(f"/spot paper-reject {proposal['id']} {code}", message)

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_valid_text_fallback_and_duplicate_are_clear(self, spot, klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, code, token = self.make(Path(directory))
            restarted = SpotGuard(settings)
            result = restarted.paper_text_approve(proposal["id"], code,
                settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            self.assertTrue(result["ok"])
            self.assertIn("simulated fill completed", result["message"])
            with self.assertRaisesRegex((PolicyError, Exception), "PENDING|claimable|not pending"):
                restarted.paper_text_approve(proposal["id"], code,
                    settings.openclaw.telegram_owner_id, settings.telegram.chat_id)

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_wrong_owner_chat_and_code(self, spot, klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, code, token = self.make(Path(directory))
            for supplied, sender, chat in [
                ("BADCODE0", settings.openclaw.telegram_owner_id, settings.telegram.chat_id),
                (code, "99999999", settings.telegram.chat_id),
                (code, settings.openclaw.telegram_owner_id, "-10099999"),
            ]:
                with self.assertRaises(SecurityError):
                    service.paper_text_approve(proposal["id"], supplied, sender, chat)

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_expired_text_approval_rejected(self, spot, klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, code, token = self.make(Path(directory))
            with service.ledger.connect() as connection:
                connection.execute("UPDATE proposals SET expires_at=? WHERE id=?",
                    (isoformat(utcnow() - timedelta(seconds=1)), proposal["id"]))
            with self.assertRaisesRegex(Exception, "PENDING|expired"):
                service.paper_text_approve(proposal["id"], code,
                    settings.openclaw.telegram_owner_id, settings.telegram.chat_id)

    def test_no_technocore_collision_or_real_write_executor(self):
        skill = (Path(__file__).parents[1] / "skills/binance-spotguard/references/workflow.md").read_text()
        self.assertIn("sg:", skill)
        self.assertNotIn("/technocore", skill.lower())
        self.assertFalse(hasattr(SpotGuard, "place_order"))
        self.assertIn("exactly one clear success/failure reply", skill)
