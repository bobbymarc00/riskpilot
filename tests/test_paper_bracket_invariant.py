from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.config import load_settings
from spotguard.intent import normalize_paper_intent
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.paper import validate_long_bracket
from spotguard.policy import PolicyError
from spotguard.service import SpotGuard
from tests.helpers import write_config

OWNER = "123456789"


def market(price: str, symbol: str = "BTCUSDT") -> SpotMarketSnapshot:
    value = Decimal(price)
    return SpotMarketSnapshot(symbol, value - Decimal("0.1"), value, value,
                              Decimal("5"), Decimal("0.001"), "TRADING", 1)


class PaperBracketInvariantTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    def fill(self, service: SpotGuard, price: str = "100", symbol: str = "BTCUSDT") -> dict:
        snap = market(price, symbol)
        with patch("spotguard.service.fetch_spot_snapshot", return_value=snap), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
            proposal = service.create_manual_buy_proposal(symbol, Decimal("6"))["proposal"]
            claim = service.claim(proposal["id"], service.signer.approval_token(proposal["canonical_json"]), OWNER, OWNER)
            return service.execute_paper(proposal["id"], claim["lease"])

    def proposal(self, service: SpotGuard, price: str) -> dict:
        snap = market(price)
        with patch("spotguard.service.fetch_spot_snapshot", return_value=snap), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
            return service.create_manual_buy_proposal("BTC", Decimal("6"))["proposal"]

    def test_inverted_or_zero_downside_is_never_low_risk(self):
        for stop in (Decimal("100"), Decimal("101")):
            with self.assertRaisesRegex(PolicyError, "stop must be below"):
                validate_long_bracket(Decimal("100"), stop, Decimal("102"), Decimal("2"))

    def test_bnb_like_invalid_existing_bracket_blocks_scale_in_before_proposal(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service)
            row = service.ledger.list_paper_positions(True)[0]
            entry = Decimal(row["average_fill_price"])
            with service.ledger.connect() as connection:
                connection.execute("UPDATE paper_positions SET final_stop=?,final_target=? WHERE id=?",
                                   (str(entry + Decimal("1")), str(entry - Decimal("2")), row["id"]))
            before = service.ledger.counts()["proposals"]
            with self.assertRaisesRegex(PolicyError, "requires repair"):
                self.proposal(service, "110")
            self.assertEqual(service.ledger.counts()["proposals"], before)
            self.assertEqual(len(service.ledger.list_paper_positions(True)), 1)

    def test_valid_bnb_like_averaging_up_preserves_stop_and_target_above_market(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service, "100")
            original = service.ledger.list_paper_positions(True)[0]
            old_stop = Decimal(original["final_stop"])
            proposal = self.proposal(service, "110")
            canonical = proposal["canonical"]
            average = Decimal(canonical["projected_weighted_average_entry"])
            target = Decimal(canonical["projected_target"])
            self.assertEqual(Decimal(canonical["projected_stop"]), old_stop)
            self.assertLess(old_stop, average); self.assertGreater(target, Decimal("110"))
            self.assertEqual((target - average) / (average - old_stop), Decimal("2"))
            claim = service.claim(proposal["id"], service.signer.approval_token(proposal["canonical_json"]), OWNER, OWNER)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=market("110")):
                service.execute_paper(proposal["id"], claim["lease"])
            rows = service.ledger.list_paper_positions(True)
            self.assertTrue(all(Decimal(row["final_stop"]) == old_stop for row in rows))
            self.assertTrue(all(Decimal(row["final_target"]) > Decimal("110") for row in rows))

    def test_eth_like_averaging_down_remains_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service, "100")
            old_stop = Decimal(service.ledger.list_paper_positions(True)[0]["final_stop"])
            proposal = self.proposal(service, "99.8")
            canonical = proposal["canonical"]
            average = Decimal(canonical["projected_weighted_average_entry"])
            target = Decimal(canonical["projected_target"])
            self.assertLess(old_stop, average); self.assertGreater(target, Decimal("99.8"))
            self.assertEqual((target - average) / (average - old_stop), Decimal("2"))

    def test_fill_drift_invalidates_bracket_atomically_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service, "100")
            proposal = self.proposal(service, "110")
            service.settings = replace(service.settings, market=replace(service.settings.market, max_entry_drift_pct=Decimal("30")))
            claim = service.claim(proposal["id"], service.signer.approval_token(proposal["canonical_json"]), OWNER, OWNER)
            before_balance = service.ledger.paper_balance(); before_rows = service.ledger.list_paper_positions(True)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=market("90")):
                with self.assertRaisesRegex(PolicyError, "stop must be below|fresh executable"):
                    service.execute_paper(proposal["id"], claim["lease"])
            failed = service.ledger.get_proposal(proposal["id"], include_private=True)
            self.assertEqual(failed["status"], "FAILED"); self.assertIsNone(failed["execution_lease_hash"])
            self.assertEqual(service.ledger.paper_balance(), before_balance)
            self.assertEqual(service.ledger.list_paper_positions(True), before_rows)

    def test_invalid_persisted_bracket_is_audited_and_not_auto_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service)
            row = service.ledger.list_paper_positions(True)[0]
            entry = Decimal(row["average_fill_price"])
            with service.ledger.connect() as connection:
                connection.execute("UPDATE paper_positions SET final_stop=?,final_target=? WHERE id=?",
                                   (str(entry + Decimal("1")), str(entry - Decimal("1")), row["id"]))
            with patch("spotguard.service.fetch_1m_candles_since") as fetch:
                result = service.monitor_paper_positions()
            fetch.assert_not_called(); self.assertFalse(result["ok"])
            self.assertIn("stop must be below", result["errors"][0]["error"])
            self.assertEqual(service.ledger.list_paper_positions(True)[0]["status"], "OPEN")
            with service.ledger.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM events WHERE kind='paper.invalid_bracket' AND entity_id=?", (row["id"],)).fetchone()[0]
            self.assertEqual(count, 1)

    def test_repair_uses_audited_stop_and_is_snapshot_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service)
            row = service.ledger.list_paper_positions(True)[0]
            valid_stop = Decimal(row["final_stop"]); entry = Decimal(row["average_fill_price"])
            with service.ledger.connect() as connection:
                connection.execute("UPDATE paper_positions SET final_stop=?,final_target=? WHERE id=?",
                                   (str(entry + Decimal("1")), str(entry - Decimal("1")), row["id"]))
            with patch("spotguard.service.fetch_spot_snapshot", return_value=market("100")):
                repaired = service.backup_and_repair_paper_bracket("BTCUSDT")
            current = service.ledger.list_paper_positions(True)[0]
            self.assertEqual(Decimal(current["final_stop"]), valid_stop)
            self.assertEqual(Decimal(current["final_target"]), entry + Decimal("2") * (entry - valid_stop))
            self.assertTrue(Path(repaired["backup"]).exists())

    def test_pososi_typo_routes_combined_paper_status(self):
        result = normalize_paper_intent("cek balance dan pososi", ("BTCUSDT",))
        self.assertEqual(result["action"], "status")
        self.assertEqual(normalize_paper_intent("live buy BTC 6", ("BTCUSDT",))["action"], "explicit_live")


if __name__ == "__main__":
    unittest.main()
