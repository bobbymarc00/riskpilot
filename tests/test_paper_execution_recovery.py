from __future__ import annotations

import tempfile
import json
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.config import load_settings
from spotguard.db import LedgerError
from spotguard.intent import normalize_paper_intent
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.policy import PolicyError
from spotguard.service import SpotGuard
from spotguard.telegram import DeliveryResult, TelegramError
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config
from tests.localization_fixtures import CASUAL_BUY, EXPLICIT_LIVE

OWNER="123456789"
MARKET=SpotMarketSnapshot("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("5"),Decimal("0.001"),"TRADING",1)

class PaperExecutionRecoveryTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    def proposal(self, service: SpotGuard, amount=Decimal("6")):
        with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET), patch(
                "spotguard.service.fetch_klines",return_value=synthetic_bullish_klines()):
            return service.create_manual_buy_proposal("BTC",amount)["proposal"]

    def claim(self, service, proposal):
        return service.claim(proposal["id"],service.signer.approval_token(proposal["canonical_json"]),OWNER,OWNER)

    def test_post_claim_policy_failure_is_terminal_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); proposal=self.proposal(service); claim=self.claim(service,proposal)
            before=service.ledger.paper_balance(); positions=service.ledger.list_paper_positions()
            with patch.object(service.ledger,"finish_paper_and_open",side_effect=LedgerError(
                    "PAPER risk rejected: current aggregate risk 3.5 USDT; new economic-position risk 1.0 USDT; projected aggregate risk 4.5 USDT; configured aggregate limit 4 USDT")):
                with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET):
                    with self.assertRaises(LedgerError): service.execute_paper(proposal["id"],claim["lease"])
            failed=service.ledger.get_proposal(proposal["id"],include_private=True)
            self.assertEqual(failed["status"],"FAILED"); self.assertIsNone(failed["execution_lease_hash"])
            self.assertIn("projected aggregate risk 4.5",failed["failure_reason"])
            self.assertEqual(service.ledger.paper_balance(),before); self.assertEqual(service.ledger.list_paper_positions(),positions)

    def test_expired_execution_without_fill_recovers_failed_and_unblocks(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); proposal=self.proposal(service); self.claim(service,proposal)
            with service.ledger.connect() as con:
                con.execute("UPDATE proposals SET execution_lease_expires_at=? WHERE id=?",(isoformat(utcnow()-timedelta(seconds=1)),proposal["id"]))
            before=service.ledger.paper_balance(); before_positions=service.ledger.list_paper_positions()
            recovered=service.ledger.recover_expired_paper_executions(proposal["id"],"aggregate-risk validation failed; no PAPER fill committed")
            self.assertEqual(recovered[0]["status"],"FAILED")
            self.assertEqual(service.ledger.paper_balance(),before); self.assertEqual(service.ledger.list_paper_positions(),before_positions)
            replacement=self.proposal(service)
            self.assertEqual(replacement["status"],"PENDING")

    def test_existing_fill_reconciles_executed_without_duplication(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); proposal=self.proposal(service); claim=self.claim(service,proposal)
            with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET): service.execute_paper(proposal["id"],claim["lease"])
            before=service.ledger.paper_balance(); count=len(service.ledger.list_paper_positions())
            with service.ledger.connect() as con:
                con.execute("UPDATE proposals SET status='EXECUTING',execution_lease_hash='expired',execution_lease_expires_at=? WHERE id=?",(isoformat(utcnow()-timedelta(seconds=1)),proposal["id"]))
            result=service.ledger.recover_expired_paper_executions(proposal["id"])
            self.assertEqual(result[0]["status"],"EXECUTED")
            self.assertEqual(len(service.ledger.list_paper_positions()),count); self.assertEqual(service.ledger.paper_balance(),before)

    def test_aggregate_projection_groups_scale_in_once_and_rejects_real_excess(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            rows=[{"symbol":"BTCUSDT","status":"OPEN","net_quantity":"1","quote_spent":"100","average_fill_price":"100","final_stop":"99.8","risk_amount":"1"},
                  {"symbol":"BTCUSDT","status":"OPEN","net_quantity":"1","quote_spent":"100","average_fill_price":"100","final_stop":"99.8","risk_amount":"1"},
                  {"symbol":"ETHUSDT","status":"OPEN","net_quantity":"1","quote_spent":"100","average_fill_price":"100","final_stop":"99.8","risk_amount":"1"}]
            plan={"net_base_quantity":"0.5","quote_spent":"50","final_stop":"99.8","risk_amount":"1"}
            with patch.object(service.ledger,"list_paper_positions",return_value=rows):
                projection=service._paper_risk_projection("BTCUSDT",Decimal("100"),plan)
            # Three tranches are grouped as two economic positions, not summed twice.
            self.assertLess(Decimal(projection["projected_aggregate_risk"]),Decimal("4"))
            rows[2]["quote_spent"]="105"
            with patch.object(service.ledger,"list_paper_positions",return_value=rows):
                with self.assertRaisesRegex(PolicyError,"current aggregate risk.*new economic-position risk.*projected aggregate risk.*configured aggregate limit"):
                    service._paper_risk_projection("BTCUSDT",Decimal("100"),plan)

    def test_approval_time_risk_failure_terminalizes(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); proposal=self.proposal(service); claim=self.claim(service,proposal)
            error=PolicyError("PAPER risk rejected: current aggregate risk 3.8 USDT; new economic-position risk 1 USDT; projected aggregate risk 4.8 USDT; configured aggregate limit 4 USDT")
            with patch.object(service,"_paper_risk_projection",side_effect=error), patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET):
                with self.assertRaises(PolicyError): service.execute_paper(proposal["id"],claim["lease"])
            self.assertEqual(service.ledger.get_proposal(proposal["id"])["status"],"FAILED")

    def test_casual_buy_is_paper_and_explicit_live_never_is(self):
        allowed=("BTCUSDT",)
        for text in ("buy me 100 usd BTC", "buy BTC 100", *CASUAL_BUY):
            value=normalize_paper_intent(text,allowed); self.assertEqual((value["action"],value["mode"]),("buy","paper"))
        for text in ("live buy BTC 6", "buy BTC 6 real", EXPLICIT_LIVE):
            self.assertEqual(normalize_paper_intent(text,allowed)["action"],"explicit_live")

    def test_delivery_failure_is_truthful_and_resend_does_not_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET), patch(
                    "spotguard.service.fetch_klines",return_value=synthetic_bullish_klines()), patch.object(
                    service.messenger,"send",side_effect=TelegramError("mock transport unavailable")):
                made=service.create_manual_buy_proposal("BTC",Decimal("6"),notify=True)
            self.assertFalse(made["notification"]["delivered"]); self.assertIn("proposal exists",made["delivery_message"])
            count=service.ledger.counts()["proposals"]
            delivery=DeliveryResult(True,"mock",{"presentation":{"blocks":[{"buttons":[{"value":"approve"},{"value":"reject"}]}]}})
            with patch.object(service.messenger,"send",return_value=delivery):
                resent=service.resend_paper_proposal(made["proposal"]["id"],OWNER,OWNER)
            self.assertTrue(resent["notification"]["delivered"]); self.assertTrue(resent["notification"]["controls_present"])
            self.assertEqual(service.ledger.counts()["proposals"],count)

    def test_balance_status_reconciles_after_realized_loss(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            with service.ledger.connect() as con:
                con.execute("UPDATE paper_account SET free_usdt='900',realized_pnl='-5',paid_fees_usdt='2' WHERE id=1")
            status=service.paper_balance_status()
            self.assertEqual(status["initial_reset_balance_usdt"],"1000")
            self.assertEqual(status["current_ledger_balance_usdt"],"900")
            self.assertEqual(status["realized_pnl_usdt"],"-5"); self.assertEqual(status["paid_fees_usdt"],"2")

    def test_fifth_distinct_position_allowed_sixth_rejected(self):
        from dataclasses import replace
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); path=write_config(root); raw=json.loads(path.read_text())
            raw["market"]["symbols"]=["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT"]
            raw["live"]["allowed_symbols"]=raw["market"]["symbols"]
            path.write_text(json.dumps(raw)); service=SpotGuard(load_settings(path))
            for symbol in ("BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT"):
                market=replace(MARKET,symbol=symbol)
                with patch("spotguard.service.fetch_spot_snapshot",return_value=market), patch(
                        "spotguard.service.fetch_klines",return_value=synthetic_bullish_klines()):
                    proposal=service.create_manual_buy_proposal(symbol,Decimal("6"))["proposal"]
                    claim=service.claim(proposal["id"],service.signer.approval_token(proposal["canonical_json"]),OWNER,OWNER)
                    service.execute_paper(proposal["id"],claim["lease"])
            self.assertEqual(service.ledger.paper_balance()["open_positions"],5)
            with self.assertRaisesRegex(PolicyError,"economic PAPER position/distinct-symbol limit reached"):
                service.create_manual_buy_proposal("ADAUSDT",Decimal("6"))

    def test_daily_entry_reset_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); service=self.service(root)
            for _ in range(10):
                proposal=self.proposal(service); claim=self.claim(service,proposal)
                with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET): service.execute_paper(proposal["id"],claim["lease"])
            with service.ledger.connect() as con:
                con.execute("UPDATE paper_positions SET status='CLOSED',closed_at=? WHERE status='OPEN'",(isoformat(),))
            restarted=SpotGuard(service.settings)
            self.assertEqual(restarted.ledger.successful_paper_entries(utcnow().date().isoformat()),10)
            with self.assertRaisesRegex(PolicyError,"daily PAPER entry quota reached"):
                self.proposal(restarted)
            yesterday=(utcnow()-timedelta(days=1)).date().isoformat()+"T12:00:00Z"
            with restarted.ledger.connect() as con:
                con.execute("UPDATE proposals SET executed_at=? WHERE mode='paper' AND status='EXECUTED'",(yesterday,))
            next_day=SpotGuard(service.settings)
            self.assertEqual(next_day.ledger.successful_paper_entries(utcnow().date().isoformat()),0)
            self.assertEqual(self.proposal(next_day)["status"],"PENDING")

    def test_proposal_time_active_tranche_revalidation(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            with patch.object(service.ledger,"paper_balance",return_value={"active_tranches":10,"open_positions":1,"free_usdt":"900"}), patch.object(
                    service.ledger,"list_paper_positions",return_value=[]):
                with self.assertRaisesRegex(PolicyError,"active PAPER tranche limit reached"):
                    service._ensure_paper_entry_available("BTCUSDT",Decimal("6"))
