from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import _dispatch_callback
from spotguard.config import load_settings
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config

SNAPSHOT = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
    Decimal("5"), Decimal("0.001"), "TRADING", 1)


class PaperApprovalRegressionTests(unittest.TestCase):
    def make(self, root: Path, notify: bool = False):
        settings = load_settings(write_config(root))
        service = SpotGuard(settings)
        created = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"), notify=notify, dry_run=True)
        proposal = created["proposal"]
        token = service.signer.approval_token(proposal["canonical_json"])
        code = service.signer.paper_confirmation_code(proposal["canonical_json"])
        return settings, service, proposal, token, code, created

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_fresh_button_callback_dispatches_and_fills(self, _spot, _klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, token, _code, _ = self.make(Path(directory))
            result = _dispatch_callback(service, f"sg:approve:{proposal['id']}:{token}",
                settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            self.assertTrue(result["ok"])
            self.assertEqual(result["proposal"]["status"], "EXECUTED")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_separate_hashes_and_notification_does_not_mutate_credentials(self, _spot, _klines):
        with tempfile.TemporaryDirectory() as directory:
            _settings, service, proposal, token, code, created = self.make(Path(directory), notify=True)
            before = service.ledger.get_proposal(proposal["id"], include_private=True)
            self.assertEqual(hashlib.sha256(token.encode("ascii")).hexdigest(), before["approval_token_hash"])
            self.assertEqual(hashlib.sha256(code.encode("ascii")).hexdigest(), before["confirmation_code_hash"])
            self.assertNotEqual(before["approval_token_hash"], before["confirmation_code_hash"])
            action = created["notification"]["payload"]["presentation"]["blocks"][0]["buttons"][0]["action"]
            self.assertEqual(action["type"], "command")
            self.assertLessEqual(len(("tgcmd:" + action["command"]).encode()), 64)
            after = service.ledger.get_proposal(proposal["id"], include_private=True)
            self.assertEqual(before["approval_token_hash"], after["approval_token_hash"])
            self.assertEqual(before["confirmation_code_hash"], after["confirmation_code_hash"])
            self.assertEqual(before["canonical_json"], after["canonical_json"])

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_exact_text_fallback_is_independent_of_invalid_callback(self, _spot, _klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, token, code, _ = self.make(Path(directory))
            wrong = ("A" if token[0] != "A" else "B") + token[1:]
            with self.assertRaises(SecurityError):
                _dispatch_callback(service, f"sg:approve:{proposal['id']}:{wrong}",
                    settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            result = service.paper_text_approve(proposal["id"], code,
                settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            self.assertTrue(result["ok"])

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_button_fallback_race_executes_once(self, _spot, _klines):
        with tempfile.TemporaryDirectory() as directory:
            settings, service, proposal, token, code, _ = self.make(Path(directory))
            barrier = threading.Barrier(2); outcomes = []
            def button():
                barrier.wait()
                try: outcomes.append(_dispatch_callback(service, f"sg:approve:{proposal['id']}:{token}", settings.openclaw.telegram_owner_id, settings.telegram.chat_id)["ok"])
                except Exception: outcomes.append(False)
            def fallback():
                barrier.wait()
                try: outcomes.append(service.paper_text_approve(proposal["id"], code, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)["ok"])
                except Exception: outcomes.append(False)
            threads=[threading.Thread(target=button), threading.Thread(target=fallback)]
            [t.start() for t in threads]; [t.join() for t in threads]
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(service.ledger.get_proposal(proposal["id"])["status"], "EXECUTED")
            self.assertEqual(len(service.ledger.list_paper_positions(open_only=True)), 1)

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT)
    def test_wrong_cross_replay_rejected_expired_and_live_fail_closed(self, _spot, _klines):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); settings, service, first, token1, code1, _ = self.make(root)
            service.paper_text_reject(first["id"], code1, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            second=service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            token2=service.signer.approval_token(second["canonical_json"]); code2=service.signer.paper_confirmation_code(second["canonical_json"])
            with self.assertRaises(Exception):
                _dispatch_callback(service, f"sg:approve:{second['id']}:{token1}", settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            with self.assertRaises(Exception):
                service.paper_text_approve(second["id"], code1, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            with service.ledger.connect() as connection:
                connection.execute("UPDATE proposals SET expires_at=? WHERE id=?", (isoformat(utcnow()-timedelta(seconds=1)), second["id"]))
            with self.assertRaises(Exception):
                service.paper_text_approve(second["id"], code2, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            with self.assertRaises(Exception):
                service.paper_text_approve(first["id"], code1, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)
            with service.ledger.connect() as connection:
                connection.execute("UPDATE proposals SET mode='live' WHERE id=?", (second["id"],))
            with self.assertRaises(Exception):
                service.paper_text_approve(second["id"], code2, settings.openclaw.telegram_owner_id, settings.telegram.chat_id)

    def test_router_keeps_exact_fallback_and_rejects_natural_language(self):
        workflow=(Path(__file__).parents[1]/"skills/binance-spotguard/references/workflow.md").read_text()
        self.assertIn("Match and route it before", workflow)
        self.assertIn("UNMODIFIED_CALLBACK_VALUE", workflow)
        self.assertIn("natural-language approval requests", workflow)
        self.assertNotIn("live-approve PROPOSAL_ID CONFIRMATION_CODE", workflow)


if __name__ == "__main__":
    unittest.main()
