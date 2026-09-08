from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import _parse_callback
from spotguard.config import load_settings
from spotguard.db import LedgerError
from spotguard.market import Kline, MarketError, SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.paper import build_fill_risk
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config

OWNER = "123456789"
SNAPSHOT = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"),
    Decimal("5"), Decimal("0.001"), "TRADING", 1)


class PaperPositionTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    def open_position(self, service: SpotGuard) -> dict:
        with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
             patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
            proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, OWNER, OWNER)
            service.execute_paper(proposal["id"], claim["lease"])
        return service.ledger.get_paper_position(f"pp-{proposal['id'][2:]}")

    def candle(self, position: dict, opened: Decimal, high: Decimal, low: Decimal) -> Kline:
        start = int(utcnow().timestamp() * 1000) - 60_000
        return Kline(start, float(opened), float(high), float(low), float(opened), 1.0, start + 59_999)

    def test_existing_fill_migration_is_idempotent_and_accounts_100_usdt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); settings = load_settings(write_config(root)); service = SpotGuard(settings)
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
                token = service.signer.approval_token(proposal["canonical_json"])
                claim = service.claim(proposal["id"], token, OWNER, OWNER)
            plan = build_fill_risk(settings, proposal, Decimal("100"), Decimal("0.06"))
            summary = {"average_fill_price": plan["average_fill_price"],
                "gross_base_quantity": plan["gross_base_quantity"], "net_base_quantity": plan["net_base_quantity"],
                "actual_paper_spend": plan["quote_spent"], "simulated_fee": plan["entry_fee_base"],
                "timestamp": isoformat()}
            service.ledger.finish_execution(proposal["id"], service.ledger.get_proposal(proposal["id"], include_private=True)["execution_lease_hash"],
                "EXECUTED", "paper-legacy", "FILLED", summary)
            restarted = SpotGuard(settings)
            self.assertEqual(len(restarted.paper_migration["migrated"]), 1)
            self.assertEqual(len(restarted.ledger.list_paper_positions(True)), 1)
            self.assertEqual(Decimal(restarted.ledger.paper_balance()["free_usdt"]), Decimal("994.0"))
            again = SpotGuard(settings)
            self.assertEqual(again.paper_migration["migrated"], [])
            self.assertEqual(len(again.ledger.list_paper_positions(True)), 1)

    def test_malformed_legacy_fill_migration_raises_ledger_error(self):
        """Startup migration must not turn an audit failure into NameError."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); settings = load_settings(write_config(root)); service = SpotGuard(settings)
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
                token = service.signer.approval_token(proposal["canonical_json"])
                service.claim(proposal["id"], token, OWNER, OWNER)
            # This has the legacy source/status shape but lacks immutable fill
            # fields. It must fail closed with the ledger domain error.
            service.ledger.finish_execution(
                proposal["id"], service.ledger.get_proposal(proposal["id"], include_private=True)["execution_lease_hash"],
                "EXECUTED", "paper-legacy", "FILLED", {"timestamp": isoformat()},
            )
            with self.assertRaisesRegex(LedgerError, "ambiguous"):
                SpotGuard(settings)

    def test_fill_based_risk_fees_reward_and_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); position = self.open_position(service)
            self.assertEqual(Decimal(position["gross_quantity"]), Decimal("0.06"))
            self.assertEqual(Decimal(position["net_quantity"]), Decimal("0.05994"))
            fill = Decimal(position["average_fill_price"]); stop = Decimal(position["final_stop"]); target = Decimal(position["final_target"])
            self.assertEqual((target - fill) / (fill - stop), Decimal("2"))
            self.assertLessEqual(Decimal(position["risk_amount"]), Decimal("1"))
            self.assertEqual(Decimal(service.ledger.paper_balance()["free_usdt"]), Decimal("994.0"))
            self.assertEqual(Decimal(service.ledger.paper_balance()["paid_fees_usdt"]), Decimal("0.006"))

    def test_stop_risk_is_worst_case_including_both_fees_and_slippage(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            proposal = {"entry_reference": "100", "stop_reference": "99"}
            plan = build_fill_risk(settings, proposal, Decimal("100"), Decimal("0.06"))
            fee_rate = settings.risk.paper_fee_pct / Decimal("100")
            slippage = settings.paper.slippage_pct / Decimal("100")
            net_quantity = Decimal("0.06") * (Decimal("1") - fee_rate)
            stop_execution = Decimal("99") * (Decimal("1") - slippage)
            expected = Decimal("6") - net_quantity * stop_execution * (Decimal("1") - fee_rate)
            self.assertEqual(Decimal(plan["risk_amount"]), expected)

    def test_take_profit_stop_gap_and_both_touched_are_conservative(self):
        cases = (("target", "TAKE_PROFIT"), ("stop", "STOP_LOSS"),
                 ("gap", "STOP_GAP"), ("both", "STOP_BOTH_TOUCHED"))
        for kind, expected in cases:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                service = self.service(Path(directory)); position = self.open_position(service)
                stop, target = Decimal(position["final_stop"]), Decimal(position["final_target"])
                if kind == "target": values = (target, target + 1, target - Decimal("0.1"))
                elif kind == "stop": values = (Decimal("100"), Decimal("101"), stop - Decimal("0.1"))
                elif kind == "gap": values = (stop - 1, stop, stop - 2)
                else: values = (Decimal("100"), target + 1, stop - 1)
                with patch("spotguard.service.fetch_1m_candles_since", return_value=[self.candle(position, *values)]):
                    result = service.monitor_paper_positions()
                closed = result["results"][0]["position"]
                self.assertEqual(closed["exit_reason"], expected)
                self.assertEqual(closed["status"], "CLOSED")
                self.assertGreater(Decimal(closed["exit_fee"]), 0)
                self.assertEqual(service.ledger.paper_balance()["open_positions"], 0)
                self.assertEqual(service.monitor_paper_positions()["results"], [])

    def test_manual_close_fallback_owner_expiry_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); position = self.open_position(service)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                created = service.create_paper_close_proposal(position["id"], OWNER, OWNER, notify=True, dry_run=True)
            payload = created["notification"]["payload"]
            message = payload["message"]
            code = message.rsplit(" ", 1)[-1]
            action = payload["presentation"]["blocks"][0]["buttons"][0]["action"]
            self.assertEqual(action["type"], "command")
            with self.assertRaises(Exception):
                service.approve_paper_close_by_position(position["id"], code, "999", OWNER)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                done = service.approve_paper_close_by_position(position["id"], code, OWNER, OWNER)
            self.assertEqual(done["position"]["status"], "CLOSED")
            with self.assertRaises(Exception):
                service.approve_paper_close_by_position(position["id"], code, OWNER, OWNER)

            second = Path(directory) / "second"; second.mkdir()
            service2 = self.service(second); pos2 = self.open_position(service2)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                close2 = service2.create_paper_close_proposal(pos2["id"], OWNER, OWNER)
            with service2.ledger.connect() as connection:
                connection.execute("UPDATE paper_close_proposals SET expires_at=? WHERE id=?",
                    (isoformat(utcnow() - timedelta(seconds=1)), close2["close_proposal"]["id"]))
            with self.assertRaisesRegex(Exception, "expired"):
                service2.approve_paper_close_by_position(pos2["id"], "WRONGCODE", OWNER, OWNER)

    def test_restart_recovery_and_incomplete_history_fail_closed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); service = self.service(root); position = self.open_position(service)
            with service.ledger.connect() as connection:
                connection.execute("UPDATE paper_positions SET status='CLOSING' WHERE id=?", (position["id"],))
            restarted = SpotGuard(service.settings)
            self.assertEqual(restarted.paper_reconciliation["positions"], 1)
            with patch("spotguard.service.fetch_1m_candles_since", side_effect=MarketError("history incomplete")):
                first = restarted.monitor_paper_positions(); second = restarted.monitor_paper_positions()
            self.assertFalse(first["ok"]); self.assertFalse(second["ok"])
            self.assertEqual(restarted.ledger.get_paper_position(position["id"])["status"], "OPEN")
            with restarted.ledger.connect() as connection:
                count = connection.execute("SELECT COUNT(*) FROM events WHERE kind='paper.exit_data_incomplete'").fetchone()[0]
            self.assertEqual(count, 1)

    def test_closed_position_releases_slot_and_scan_still_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); position = self.open_position(service)
            target = Decimal(position["final_target"])
            with patch("spotguard.service.fetch_1m_candles_since", return_value=[self.candle(position, target, target + 1, target)]):
                service.monitor_paper_positions()
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                proposal = service.create_manual_buy_proposal("BTCUSDT", Decimal("6"))["proposal"]
            self.assertEqual(proposal["status"], "PENDING")
            with patch.object(service, "monitor_paper_positions", return_value={"ok": True, "results": [], "errors": []}), \
                 patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
                scan = service.scan(symbols=["BTCUSDT"], synthetic=True)
            self.assertEqual(len(scan["results"]), 1)

    def test_configured_ten_tranche_limit_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = write_config(root)
            raw = json.loads(config.read_text())
            raw["paper"]["max_active_tranches"] = 10
            config.write_text(json.dumps(raw))
            service = SpotGuard(load_settings(config))
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT):
                for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "BNBUSDT", "BTCUSDT", "ETHUSDT", "BNBUSDT", "BTCUSDT"):
                    proposal = service.create_manual_buy_proposal(symbol, Decimal("5"))["proposal"]
                    token = service.signer.approval_token(proposal["canonical_json"])
                    claim = service.claim(proposal["id"], token, OWNER, OWNER)
                    service.execute_paper(proposal["id"], claim["lease"])
                self.assertEqual(service.ledger.paper_balance()["active_tranches"], 10)
                with self.assertRaisesRegex(Exception, "active PAPER tranche limit"):
                    service.create_manual_buy_proposal("BTCUSDT", Decimal("5"))

    def test_routes_and_source_remain_isolated(self):
        workflow = (Path(__file__).parents[1] / "skills/binance-spotguard/references/workflow.md").read_text()
        for command in ("/spot paper balance", "/spot paper positions", "/spot paper-close POSITION_ID",
                        "/spot paper-close-approve POSITION_ID CONFIRMATION_CODE"):
            self.assertIn(command, workflow)
        self.assertNotIn("/technocore", workflow.lower())
        source = "\n".join(p.read_text() for p in (Path(__file__).parents[1] / "src/spotguard").glob("*.py"))
        for forbidden in ("futures/order", "margin/order", "withdraw/apply", "universalTransfer"):
            self.assertNotIn(forbidden, source)
        self.assertFalse(hasattr(SpotGuard, "place_order"))


if __name__ == "__main__":
    unittest.main()
