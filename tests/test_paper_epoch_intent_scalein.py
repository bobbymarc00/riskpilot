from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.cli import main
from spotguard.config import load_settings
from spotguard.intent import normalize_paper_intent
from spotguard.market import SpotMarketSnapshot, synthetic_bullish_klines
from spotguard.paper import build_fill_risk
from spotguard.service import SpotGuard
from spotguard.util import isoformat, utcnow
from tests.helpers import write_config
from tests.localization_fixtures import BALANCE, BUY_QUESTIONS, BUY_VARIANTS, CLOSE_ALL, POSITIONS

OWNER = "123456789"
SNAPSHOT = SpotMarketSnapshot("BTCUSDT", Decimal("99.9"), Decimal("100"), Decimal("100"), Decimal("5"), Decimal("0.001"), "TRADING", 1)

class PaperEpochIntentScaleInTests(unittest.TestCase):
    def service(self, root: Path) -> SpotGuard:
        return SpotGuard(load_settings(write_config(root)))

    def fill(self, service: SpotGuard, symbol="BTCUSDT", amount=Decimal("6")):
        snap = replace(SNAPSHOT, symbol=symbol)
        with patch("spotguard.service.fetch_spot_snapshot", return_value=snap), patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()):
            proposal = service.create_manual_buy_proposal(symbol, amount)["proposal"]
            token = service.signer.approval_token(proposal["canonical_json"])
            claim = service.claim(proposal["id"], token, OWNER, OWNER)
            return service.execute_paper(proposal["id"], claim["lease"])

    def test_decimal_rr_reanchors_three_observed_examples(self):
        with tempfile.TemporaryDirectory() as d:
            settings = load_settings(write_config(Path(d)))
            for entry, stop, fill, expected in (
                ("78488", "78095.56", "78528.01", "79312.89"),
                ("711.51", "707.70186868", "712.25", "719.86626264"),
                ("2424.97", "2412.84515", "2423.82", "2448.06970"),
            ):
                proposal={"entry_reference":entry,"stop_reference":stop}
                result=build_fill_risk(settings, proposal, Decimal(fill), Decimal("0.00001"))
                self.assertEqual(Decimal(result["final_target"]), Decimal(expected))
                self.assertEqual(Decimal(result["final_stop"]), Decimal(fill)-(Decimal(entry)-Decimal(stop)))

    def test_stale_close_cleanup_replacement_restart_and_replay(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); service=self.service(root); self.fill(service)
            position=service.ledger.list_paper_positions(True)[0]
            with patch("spotguard.service.fetch_spot_snapshot",return_value=SNAPSHOT):
                old=service.create_paper_close_proposal(position["id"],OWNER,OWNER)["close_proposal"]
            with service.ledger.connect() as con:
                con.execute("UPDATE paper_close_proposals SET expires_at=? WHERE id=?",(isoformat(utcnow()-timedelta(seconds=1)),old["id"]))
            with patch("spotguard.service.fetch_spot_snapshot",return_value=SNAPSHOT):
                fresh_result=service.create_paper_close_proposal(position["id"],OWNER,OWNER,notify=True,dry_run=True)
            fresh=fresh_result["close_proposal"]
            fresh_code=fresh_result["notification"]["payload"]["message"].rsplit(" ",1)[-1]
            self.assertEqual(service.ledger.get_paper_close_proposal(old["id"])["status"],"EXPIRED")
            with self.assertRaises(Exception): service.approve_paper_close(old["id"],OWNER,OWNER,token="wrong")
            restarted=SpotGuard(service.settings)
            self.assertEqual(restarted.ledger.get_paper_close_proposal(fresh["id"])["status"],"PENDING")
            private=restarted.ledger.get_paper_close_proposal(fresh["id"],True)
            with patch("spotguard.service.fetch_spot_snapshot",return_value=SNAPSHOT):
                restarted.approve_paper_close_by_position(position["id"],fresh_code,OWNER,OWNER)
            with self.assertRaises(Exception): restarted.approve_paper_close_by_position(position["id"],fresh_code,OWNER,OWNER)

    def test_intent_variants_questions_conditionals_and_multi_action(self):
        allowed=("BTCUSDT","BNBUSDT","SOLUSDT")
        for text in ("buy BTC 6", *BUY_VARIANTS, "buy 6 USDT BTC", "paper buy BTC 6", "add BTC 6"):
            result=normalize_paper_intent(text,allowed); self.assertEqual(result["action"],"buy",text); self.assertEqual(result["mode"],"paper")
        for text in (*BUY_QUESTIONS, "> buy BTC 6", '"buy BTC 6"'):
            self.assertNotEqual(normalize_paper_intent(text,allowed)["action"],"buy",text)
        self.assertEqual(normalize_paper_intent("approve",allowed)["action"],"forbidden_approval")
        multi=normalize_paper_intent("close BNB dan buy SOL 6",allowed)
        self.assertEqual((multi["action"],multi["symbol"],multi["buy_deferred"]),("close","BNBUSDT",True))
        self.assertEqual(normalize_paper_intent(POSITIONS,allowed)["action"],"positions")
        self.assertEqual(normalize_paper_intent(BALANCE,allowed)["action"],"balance")

    def test_relaxed_buy_and_close_selectors_are_paper_only(self):
        allowed=("BTCUSDT","BNBUSDT")
        for text in ("buy BTC 10", "buy paper BTC 10", "paper buy BTC 10",
                     "buy 10 usd BTC", "buy me 10 usd of BTC"):
            intent=normalize_paper_intent(text, allowed)
            self.assertEqual((intent["action"], intent["mode"], intent["quote_amount"]), ("buy", "paper", "10"))
        self.assertEqual(normalize_paper_intent(CLOSE_ALL, allowed)["close_selector"], "all")
        self.assertEqual(normalize_paper_intent("close all BTC", allowed)["close_selector"], "all")
        self.assertEqual(normalize_paper_intent("sell 40% BTC", allowed)["percentage"], "40")
        self.assertEqual(normalize_paper_intent("sell 0.0005 BTC", allowed)["close_quantity"], "0.0005")
        self.assertEqual(normalize_paper_intent("sell 20 usd of BNB", allowed)["close_quote_amount"], "20")
        for text in ("buy BTC 0", "buy BTC -1", "sell 0 BTC", "sell -20% BTC", "sell 101% BTC"):
            self.assertEqual(normalize_paper_intent(text, allowed)["action"], "invalid", text)
        self.assertEqual(normalize_paper_intent("buy live BTC 10", allowed)["action"], "explicit_live")

    def test_cli_routing_bare_buy_uses_quote_usdt_without_preconfirmation(self):
        """Exercise the Telegram skill's canonical paper-intent CLI contract, not just its parser."""
        for utterance in ("buy BTC 10", "buy 10 BTC", "buy BTC 10 usd"):
            with self.subTest(utterance=utterance), tempfile.TemporaryDirectory() as directory:
                config = write_config(Path(directory)); stdout = io.StringIO()
                with patch("spotguard.service.fetch_spot_snapshot", return_value=SNAPSHOT), patch(
                        "spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), contextlib.redirect_stdout(stdout):
                    code = main(["--config", str(config), "--json", "paper-intent", "--text", utterance,
                                 "--sender-id", OWNER, "--chat-id", OWNER, "--notify", "--dry-run"])
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual(result["intent"], {
                    "action": "buy", "symbol": "BTCUSDT", "quote_amount": "10", "mode": "paper"})
                self.assertEqual((result["proposal"]["mode"], result["proposal"]["quote_amount"],
                                  result["proposal"]["status"]), ("paper", "10", "PENDING"))
                self.assertEqual(result["notification"]["transport"], "dry-run")

        for utterance in ("buy BTC", "buy BTC 10 20", "buy BTC ten", "buy DOGE 10", "buy live BTC 10"):
            with self.subTest(rejected=utterance), tempfile.TemporaryDirectory() as directory:
                config = write_config(Path(directory)); stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = main(["--config", str(config), "--json", "paper-intent", "--text", utterance,
                                 "--sender-id", OWNER, "--chat-id", OWNER, "--notify", "--dry-run"])
                self.assertEqual(code, 0)
                result = json.loads(stdout.getvalue())
                self.assertFalse(result["ok"])
                self.assertNotIn("proposal", result)
                self.assertNotEqual(result["intent"]["action"], "buy")

    def test_scale_in_weighted_entry_stop_and_full_close(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); self.fill(service); first=service.ledger.list_paper_positions(True)[0]
            old_stop=Decimal(first["final_stop"])
            result=self.fill(service)
            rows=service.ledger.list_paper_positions(True)
            self.assertEqual(len(rows),2); self.assertEqual(service.ledger.paper_balance()["open_positions"],1)
            self.assertEqual(service.ledger.paper_balance()["active_tranches"],2)
            self.assertTrue(all(Decimal(row["final_stop"]) >= old_stop for row in rows))
            status=service.paper_status(); self.assertEqual(status["positions"][0]["active_tranche_count"],2)
            self.assertIsNotNone(result["execution_summary"]["economic_position"])
            with patch("spotguard.service.fetch_spot_snapshot",return_value=SNAPSHOT):
                close_result=service.create_paper_close_proposal(rows[0]["id"],OWNER,OWNER,notify=True,dry_run=True)
            close=close_result["close_proposal"]
            close_code=close_result["notification"]["payload"]["message"].rsplit(" ",1)[-1]
            with patch("spotguard.service.fetch_spot_snapshot",return_value=SNAPSHOT):
                done=service.approve_paper_close_by_position(rows[0]["id"],close_code,OWNER,OWNER)
            self.assertEqual(len(done["closed_tranches"]),2)
            self.assertEqual(service.ledger.paper_balance()["active_tranches"],0)
            with self.assertRaises(Exception): service.approve_paper_close_by_position(rows[0]["id"],close_code,OWNER,OWNER)

    def test_daily_realized_loss_cap_is_enforced(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            with patch.object(service.ledger,"daily_paper_realized_loss",return_value=Decimal("5")):
                with self.assertRaisesRegex(Exception,"daily realized loss"):
                    service._ensure_paper_entry_available("BTCUSDT",Decimal("6"))

    def test_reset_atomic_clean_and_historical_evidence_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d)); self.fill(service)
            before_events=service.ledger.counts(); before_rows=len(service.ledger.list_paper_positions())
            with self.assertRaises(Exception): service.ledger.reset_paper_account(Decimal("0"),"bad")
            result=service.ledger.reset_paper_account(Decimal("1000"),"hackathon demo reset")
            balance=result["current"]
            self.assertEqual((balance["free_usdt"],balance["locked_usdt"],balance["realized_pnl"],balance["paid_fees_usdt"]),("1000","0","0","0"))
            self.assertEqual((balance["assets"],balance["open_positions"],balance["active_tranches"]),({},0,0))
            self.assertEqual(len(service.ledger.list_paper_positions()),before_rows)
            with service.ledger.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM events WHERE kind='paper.account_reset'").fetchone()[0],1)
                self.assertGreater(con.execute("SELECT COUNT(*) FROM events").fetchone()[0],before_events["proposals"])

    def test_reset_cli_requires_tty_owner_phrase_and_is_not_telegram_route(self):
        with tempfile.TemporaryDirectory() as d:
            config=write_config(Path(d)); out=io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertNotEqual(main(["--config",str(config),"--json","paper-reset","--owner-id",OWNER]),0)
            with patch("sys.stdin.isatty",return_value=True),patch("builtins.input",return_value="WRONG"):
                self.assertNotEqual(main(["--config",str(config),"--json","paper-reset","--owner-id",OWNER]),0)
            with patch("sys.stdin.isatty",return_value=True),patch("builtins.input",return_value="RESET RISK PILOT PAPER ACCOUNT TO 1000 USDT"):
                self.assertNotEqual(main(["--config",str(config),"--json","paper-reset","--owner-id","wrong"]),0)
            with SpotGuard(load_settings(config)).ledger.connect() as con:
                con.execute("UPDATE paper_account SET free_usdt='900' WHERE id=1")
            with patch("sys.stdin.isatty",return_value=True),patch("builtins.input",return_value="RESET RISK PILOT PAPER ACCOUNT TO 1000 USDT"):
                self.assertEqual(main(["--config",str(config),"--json","paper-reset","--owner-id",OWNER]),0)
            checked=SpotGuard(load_settings(config)).ledger.paper_balance()
            self.assertEqual((checked["free_usdt"],checked["open_positions"],checked["active_tranches"]),("1000",0,0))
            workflow=(Path(__file__).parents[1]/"skills/binance-spotguard/references/workflow.md").read_text()
            self.assertNotIn("/spot paper-reset",workflow)

    def test_sqlite_backup_uses_distinct_safe_target_on_timestamp_collision(self):
        with tempfile.TemporaryDirectory() as d:
            service=self.service(Path(d))
            first=service.ledger.backup("pre-paper-reset")
            second=service.ledger.backup("pre-paper-reset")
            self.assertNotEqual(first,second)
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())
            for path in (first,second):
                with sqlite3.connect(path) as connection:
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0],"ok")

if __name__ == "__main__": unittest.main()
