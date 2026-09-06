from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from spotguard.config import load_settings
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.policy import PolicyError
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config


def market(ask: str = "100", minimum: str = "5", step: str = "0.001") -> SpotMarketSnapshot:
    return SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal(ask), Decimal(ask),
        Decimal(minimum), Decimal(step), "TRADING", 1)


class ManualFlowsTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_six_usdt_proposal_fill_math_and_no_real_executor(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            created = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))
            proposal = created["proposal"]
            self.assertEqual(proposal["canonical"]["source"], "manual-paper-test")
            self.assertTrue(proposal["canonical"]["approval_nonce"])
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, "123456789", "123456789")
            filled = service.execute_paper(proposal["id"], claim["lease"])
            summary = filled["execution_summary"]
            self.assertEqual(summary["warning"], "MANUAL PAPER TEST — NOT A REAL ORDER")
            self.assertEqual(Decimal(summary["gross_base_quantity"]), Decimal("0.06"))
            self.assertEqual(Decimal(summary["simulated_fee"]), Decimal("0.00006"))
            self.assertEqual(Decimal(summary["net_base_quantity"]), Decimal("0.05994"))
            self.assertEqual(Decimal(summary["actual_paper_spend"]), Decimal("6.0"))
            self.assertEqual(summary["paper_order_id"], filled["execution_order_id"])
            self.assertFalse(hasattr(service, "place_order"))
            with service.ledger.connect() as connection:
                kinds = {r[0] for r in connection.execute("SELECT kind FROM events")}
            self.assertTrue({"proposal.created", "proposal.claimed", "paper.fill", "execution.completed"} <= kinds)

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_policy_sender_symbol_amount_and_replay(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            with self.assertRaises(SpotGuardError):
                service.create_manual_buy_proposal("XRPUSDT", Decimal("6"))
            with self.assertRaises(PolicyError):
                service.create_manual_buy_proposal("BTCUSDT", Decimal("101"))
            made = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            token = service.signer.approval_token(made["canonical_json"])
            with self.assertRaises(SecurityError):
                service.claim(made["id"], token, "99999999", "123456789")
            with self.assertRaises(SecurityError):
                service.claim(made["id"], token, "123456789", "-10012345")
            claim = service.claim(made["id"], token, "123456789", "123456789")
            with self.assertRaisesRegex(Exception, "PENDING|not claimable"):
                service.claim(made["id"], token, "123456789", "123456789")
            self.assertTrue(claim["lease"])

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market(minimum="11"))
    def test_min_notional_after_step_rounding(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(PolicyError, "minimum notional"):
                self.service(Path(directory)).create_manual_buy_proposal("BTCUSDT", Decimal("6"))

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_expired_active_cleanup_and_scheduled_regression(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            first = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            with service.ledger.connect() as connection:
                connection.execute("UPDATE proposals SET expires_at=? WHERE id=?",
                    (isoformat(utcnow() - timedelta(seconds=1)), first["id"]))
            second = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            self.assertEqual(service.ledger.get_proposal(first["id"])["status"], "EXPIRED")
            service.reject(second["id"], service.signer.approval_token(second["canonical_json"]), "123456789")
            scan = service.scan(symbols=["ETHUSDT"], synthetic=True)
            self.assertIsNotNone(scan["results"][0]["candidate"])
            self.assertNotEqual(scan["results"][0]["candidate"]["metrics"].get("source"), "manual-paper-test")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", side_effect=[market(), market("102")])
    def test_price_drift_rejection(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, "123456789", "123456789")
            with self.assertRaisesRegex(PolicyError, "requote"):
                service.execute_paper(proposal["id"], claim["lease"])
            self.assertEqual(service.ledger.get_proposal(proposal["id"])["status"], "FAILED")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_expired_active_demo_is_cleaned_before_paper_buy(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            demo = service.create_demo_candidate("BTCUSDT", Decimal("100"))["candidate"]
            with service.ledger.connect() as connection:
                connection.execute("UPDATE candidates SET expires_at=? WHERE id=?",
                    ("2020-01-01T00:00:00+00:00", demo["id"]))
            service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))
            self.assertEqual(service.ledger.get_candidate(demo["id"])["status"], "EXPIRED")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_future_pending_still_blocks(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))
            with self.assertRaisesRegex(Exception, "maximum active"):
                service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))

    def test_utc_z_offset_and_naive_existing_timestamps(self) -> None:
        from spotguard.util import parse_time
        self.assertEqual(parse_time("2026-09-02T10:00:00Z"),
                         parse_time("2026-09-02T10:00:00+00:00"))
        self.assertEqual(parse_time("2026-09-02 10:00:00"),
                         parse_time("2026-09-02T10:00:00Z"))

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_executed_and_rejected_are_never_auto_expired(self, spot, klines) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            first = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            service.reject(first["id"], service.signer.approval_token(first["canonical_json"]), "123456789")
            second = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            token = service.signer.approval_token(second["canonical_json"])
            claim = service.claim(second["id"], token, "123456789", "123456789")
            service.execute_paper(second["id"], claim["lease"])
            with service.ledger.connect() as connection:
                connection.execute("UPDATE proposals SET expires_at=? WHERE id IN (?,?)",
                    ("2020-01-01T00:00:00Z", first["id"], second["id"]))
            service.ledger.expire_stale_active_proposals()
            self.assertEqual(service.ledger.get_proposal(first["id"])["status"], "REJECTED")
            self.assertEqual(service.ledger.get_proposal(second["id"])["status"], "EXECUTED")

    @patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines())
    @patch("spotguard.service.fetch_spot_snapshot", return_value=market())
    def test_concurrent_creation_cannot_exceed_limit(self, spot, klines) -> None:
        import threading
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = load_settings(write_config(root))
            barrier = threading.Barrier(2)
            outcomes = []
            services = [SpotGuard(settings), SpotGuard(settings)]
            def create(service) -> None:
                barrier.wait()
                try:
                    outcomes.append(("ok", service.create_manual_buy_proposal(
                        "BTCUSDT", Decimal("6"))["proposal"]["id"]))
                except Exception as exc:
                    outcomes.append(("error", str(exc)))
            threads = [threading.Thread(target=create, args=(service,)) for service in services]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(sum(kind == "ok" for kind, _ in outcomes), 1)
            self.assertEqual(SpotGuard(settings).ledger.active_proposal_count(), 1)

    def test_live_disabled_disarmed_and_protection_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            with self.assertRaisesRegex(SecurityError, "disabled"):
                service.create_manual_buy_proposal("BTCUSDT", Decimal("6"), live=True)


from spotguard.service import SpotGuardError
