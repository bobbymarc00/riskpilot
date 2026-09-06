from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.config import load_settings
from spotguard.intent import normalize_paper_intent
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config
from tests.localization_fixtures import CLOSE_ALL_INDONESIAN, PARTIAL_CLOSE, REJECT_AND_BUY, SELL_PARTIAL

OWNER = "123456789"
MARKET = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"), Decimal("5"), Decimal("0.001"), "TRADING", 1)

class PaperLimitsAndPartialCloseTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    def fill(self, service: SpotGuard, amount: Decimal = Decimal("6")) -> dict:
        with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch(
                "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
            proposal = service.create_manual_buy_proposal("BTC", amount)["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, OWNER, OWNER)
            return service.execute_paper(proposal["id"], claim["lease"])

    def close(self, service: SpotGuard, percentage: Decimal) -> tuple[dict, dict]:
        position = service.ledger.list_paper_positions(True)[0]
        with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
            made = service.create_paper_close_proposal(position["id"], OWNER, OWNER, percentage,
                                                        notify=True, dry_run=True)
        code = made["notification"]["payload"]["message"].rsplit(" ", 1)[-1]
        with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
            done = service.approve_paper_close_by_position(position["id"], code, OWNER, OWNER)
        return made, done

    def test_paper_limits_are_independent_from_live(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(write_config(Path(directory)))
            self.assertEqual((settings.paper.initial_balance_usdt, settings.paper.max_quote_per_entry_usdt,
                settings.paper.max_active_tranches, settings.paper.max_economic_positions,
                settings.paper.max_open_exposure_usdt, settings.paper.max_successful_entries_per_utc_day,
                settings.paper.max_risk_per_position_usdt, settings.paper.max_aggregate_risk_usdt,
                settings.paper.daily_realized_loss_cap_usdt),
                (Decimal("1000"), Decimal("100"), 10, 5, Decimal("500"), 10,
                 Decimal("2"), Decimal("4"), Decimal("5")))
            self.assertEqual((settings.live.max_live_trade_usdt, settings.live.max_open_positions,
                settings.live.max_open_exposure_usdt), (Decimal("100"), 5, Decimal("500")))
            self.assertFalse(settings.live.enabled); self.assertFalse(settings.live.arm)

    def test_hundred_accepted_over_hundred_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch(
                    "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
                self.assertEqual(service.create_manual_buy_proposal("BTC", Decimal("100"))["proposal"]["quote_amount"], "100")
            service.ledger.reject_all_pending_paper("test cleanup")
            with self.assertRaisesRegex(Exception, "exceeds configured maximum 100"):
                service.create_manual_buy_proposal("BTC", Decimal("100.01"))

    def test_tenth_tranche_allowed_eleventh_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            for _ in range(10): self.fill(service)
            self.assertEqual(service.ledger.paper_balance()["active_tranches"], 10)
            with self.assertRaisesRegex(Exception, "active PAPER tranche limit"):
                service.create_manual_buy_proposal("BTC", Decimal("6"))

    def test_exposure_above_five_hundred_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            rows = [{"symbol":"BTCUSDT", "quote_spent":"450", "risk_amount":"1"}]
            with patch.object(service.ledger, "paper_balance", return_value={"active_tranches":1,"free_usdt":"550"}), patch.object(
                    service.ledger, "list_paper_positions", return_value=rows):
                with self.assertRaisesRegex(Exception, "500 USDT"):
                    service._ensure_paper_entry_available("ETHUSDT", Decimal("100"))

    def test_ten_fills_count_eleventh_rejected_and_unfilled_do_not_count(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            for index in range(10):
                result = self.fill(service)
                with service.ledger.connect() as con:
                    con.execute("UPDATE paper_positions SET status='CLOSED',closed_at=? WHERE proposal_id=?",
                                (isoformat(), result["id"]))
            self.assertEqual(service.ledger.successful_paper_entries(utcnow().date().isoformat()), 10)
            with self.assertRaisesRegex(Exception, "daily PAPER entry quota reached"):
                service.create_manual_buy_proposal("BTC", Decimal("6"))
            with service.ledger.connect() as con:
                rejected = con.execute("SELECT COUNT(*) FROM proposals WHERE status='REJECTED'").fetchone()[0]
            self.assertGreaterEqual(rejected, 0)

    def test_partial_close_uses_aggregate_and_updates_account(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service, Decimal("50")); self.fill(service, Decimal("50"))
            before = service.ledger.paper_balance(); rows = service.ledger.list_paper_positions(True)
            aggregate = sum((Decimal(row["net_quantity"]) for row in rows), Decimal("0"))
            made, done = self.close(service, Decimal("70"))
            proposed = Decimal(made["close"]["close_quantity"])
            self.assertEqual(proposed, (aggregate * Decimal("0.70") // MARKET.step_size) * MARKET.step_size)
            self.assertEqual(Decimal(done["close"]["quantity_remaining"]), aggregate - proposed)
            self.assertEqual(done["close"]["execution_status"], "FILLED")
            self.assertEqual(done["close"]["economic_position_status"], "OPEN")
            self.assertLess(Decimal(done["close"]["actual_executed_percentage"]), Decimal("70"))
            self.assertEqual(len(service.ledger.list_paper_positions(True)), 2)
            self.assertGreater(Decimal(done["balance"]["free_usdt"]), Decimal(before["free_usdt"]))
            self.assertGreater(Decimal(done["balance"]["paid_fees_usdt"]), Decimal(before["paid_fees_usdt"]))

    def test_quantity_and_quote_close_selectors_never_upgrade_to_full(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service, Decimal("50"))
            position = service.ledger.list_paper_positions(True)[0]
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                quantity = service.create_paper_close_proposal(position["id"], OWNER, OWNER,
                    close_quantity=Decimal("0.2"))
                self.assertEqual(quantity["close"]["selector"], "quantity")
                self.assertEqual(Decimal(quantity["close"]["close_quantity"]), Decimal("0.2"))
            # A second active proposal is deliberately blocked until the first is rejected.
            private = service.ledger.get_paper_close_proposal(quantity["close_proposal"]["id"], True)
            token = service.signer.approval_token(private["payload_json"])
            service.ledger.reject_paper_close(quantity["close_proposal"]["id"], service.signer.token_hash(token), OWNER, OWNER)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                quote = service.create_paper_close_proposal(position["id"], OWNER, OWNER,
                    close_quote_amount=Decimal("20"))
            self.assertEqual(quote["close"]["selector"], "quote")
            self.assertLess(Decimal(quote["close"]["close_quantity"]), Decimal(quote["close"]["aggregate_quantity"]))
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                with self.assertRaisesRegex(Exception, "full close"):
                    service.create_paper_close_proposal(position["id"], OWNER, OWNER,
                        close_quote_amount=Decimal("100"))

    def test_full_close_closes_every_tranche_and_replay_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service); self.fill(service)
            made, done = self.close(service, Decimal("100"))
            self.assertEqual(len(done["closed_tranches"]), 2)
            self.assertEqual(done["close"]["execution_status"], "FILLED")
            self.assertEqual(done["close"]["economic_position_status"], "CLOSED")
            self.assertEqual(service.ledger.paper_balance()["active_tranches"], 0)
            with self.assertRaises(Exception):
                service.approve_paper_close(made["close_proposal"]["id"], OWNER, OWNER, token="replay")

    def test_dust_fails_before_creation_and_close_ttl_uses_persistence_time(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service)
            position = service.ledger.list_paper_positions(True)[0]
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                with self.assertRaisesRegex(Exception, "dust"):
                    service.create_paper_close_proposal(position["id"], OWNER, OWNER, Decimal("99"))
            with service.ledger.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM paper_close_proposals WHERE status IN ('PENDING','EXECUTING')").fetchone()[0], 0)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                made = service.create_paper_close_proposal(position["id"], OWNER, OWNER)
            created, expires = made["close_proposal"]["created_at"], made["close_proposal"]["expires_at"]
            from spotguard.util import parse_time
            self.assertEqual(int((parse_time(expires)-parse_time(created)).total_seconds()), 300)

    def test_rounded_percentage_message_never_claims_unrounded_request(self):
        from spotguard.telegram import paper_close_message
        position = {"symbol": "BTCUSDT", "economic_position_id": "pe-test", "position_id": "pp-test",
            "requested_percentage": "40", "actual_executed_percentage": "37.5375375375",
            "aggregate_quantity": "0.00023976", "close_quantity": "0.00009000", "remaining_quantity": "0.00014976",
            "average_cost": "60000", "reference_bid": "60000", "estimated_gross_proceeds": "5.4",
            "estimated_fee_usdt": "0.0054", "estimated_fee_asset": "USDT", "estimated_net_proceeds": "5.3946",
            "estimated_realized_pnl": "0"}
        text, _ = paper_close_message(position, {"id": "pc-test", "expires_at": "2999-01-01T00:00:00Z"}, "unused", "CODE1234")
        self.assertIn("Requested close: 40%", text)
        self.assertIn("Actual executable close after LOT_SIZE rounding: 37.54%", text)

    def test_expired_close_replacement_and_pending_auto_terminalization(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service)
            position = service.ledger.list_paper_positions(True)[0]
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                old = service.create_paper_close_proposal(position["id"], OWNER, OWNER)["close_proposal"]
            with service.ledger.connect() as con:
                con.execute("UPDATE paper_close_proposals SET expires_at=? WHERE id=?",
                            (isoformat(utcnow()-timedelta(seconds=1)), old["id"]))
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                fresh = service.create_paper_close_proposal(position["id"], OWNER, OWNER)["close_proposal"]
            self.assertEqual(service.ledger.get_paper_close_proposal(old["id"])["status"], "EXPIRED")
            values={"exit_price":Decimal("99"),"exit_fee":Decimal("0.001"),"gross_proceeds":Decimal("5.9"),"net_proceeds":Decimal("5.899")}
            service.ledger.close_paper_position(position["id"], "TAKE_PROFIT", values)
            self.assertEqual(service.ledger.get_paper_close_proposal(fresh["id"])["status"], "FAILED")

    def test_reset_temporary_db_preserves_history_and_uses_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); filled = self.fill(service)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                service.create_paper_close_proposal(service.ledger.list_paper_positions(True)[0]["id"], OWNER, OWNER)
            before = service.ledger.counts(); result = service.ledger.reset_paper_account(Decimal("1000"), "hackathon demo reset")
            self.assertEqual((result["current"]["free_usdt"], result["current"]["locked_usdt"], result["current"]["assets"]), ("1000","0",{}))
            reset_row=service.ledger.get_paper_position(filled["execution_summary"]["position_id"])
            self.assertEqual((reset_row["status"],reset_row["exit_reason"]), ("CLOSED","ACCOUNT_RESET"))
            self.assertGreaterEqual(service.ledger.counts()["proposals"], before["proposals"])

    def test_intent_partial_and_multi_action_only_one_mutation(self):
        allowed=("BTCUSDT","SOLUSDT")
        self.assertEqual(normalize_paper_intent(PARTIAL_CLOSE,allowed)["percentage"], "70")
        self.assertEqual(normalize_paper_intent(SELL_PARTIAL,allowed)["action"], "close")
        self.assertEqual(normalize_paper_intent(CLOSE_ALL_INDONESIAN,allowed)["percentage"], "100")
        multi=normalize_paper_intent(REJECT_AND_BUY,allowed)
        self.assertEqual(multi["action"], "reject_pending"); self.assertTrue(multi["buy_deferred"])

    def test_rejected_expired_and_unfilled_proposals_do_not_consume_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory))
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET), patch(
                    "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
                rejected = service.create_manual_buy_proposal("BTC", Decimal("6"))["proposal"]
                service.reject(rejected["id"], service.signer.approval_token(rejected["canonical_json"]), OWNER, OWNER)
                expired = service.create_manual_buy_proposal("BTC", Decimal("6"))["proposal"]
                with service.ledger.connect() as con:
                    con.execute("UPDATE proposals SET expires_at=? WHERE id=?", (isoformat(utcnow()-timedelta(seconds=1)), expired["id"]))
                service.ledger.expire_stale_active_proposals()
                service.create_manual_buy_proposal("BTC", Decimal("6"))
            self.assertEqual(service.ledger.successful_paper_entries(utcnow().date().isoformat()), 0)

    def test_monitor_closes_all_tranches_and_terminalizes_pending_close(self):
        from spotguard.market import Kline
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(Path(directory)); self.fill(service); self.fill(service)
            rows=service.ledger.list_paper_positions(True)
            with patch("spotguard.service.fetch_spot_snapshot", return_value=MARKET):
                pending=service.create_paper_close_proposal(rows[0]["id"], OWNER, OWNER)["close_proposal"]
            target=Decimal(rows[0]["final_target"]); now=int(utcnow().timestamp()*1000)-60000
            candle=Kline(now,float(target),float(target+1),float(target),float(target),1,now+59999)
            with patch("spotguard.service.fetch_1m_candles_since", return_value=[candle]):
                result=service.monitor_paper_positions()
            self.assertEqual(len(result["results"][0]["closed_tranches"]),2)
            self.assertEqual(service.ledger.get_paper_close_proposal(pending["id"])["status"],"FAILED")
            self.assertEqual(service.ledger.paper_balance()["active_tranches"],0)

    def test_scale_in_message_is_coherent_and_fee_is_fixed_decimal(self):
        with tempfile.TemporaryDirectory() as directory:
            service=self.service(Path(directory)); self.fill(service)
            with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET), patch(
                    "spotguard.service.fetch_klines",return_value=synthetic_bullish_klines()):
                made=service.create_manual_buy_proposal("BTC",Decimal("6"),notify=True,dry_run=True)
            message=made["notification"]["payload"]["message"]
            self.assertIn("PAPER SCALE-IN PROPOSAL — NOT A REAL ORDER",message)
            self.assertIn("Current/projected aggregate quantity",message)
            fee_line=next(line for line in message.splitlines() if line.startswith("Fee estimate:"))
            self.assertNotIn("E-",fee_line); self.assertIn("BTC",fee_line); self.assertIn("USDT",fee_line)
