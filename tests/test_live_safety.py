from __future__ import annotations
import json, tempfile, unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch
from spotguard.config import load_settings
from spotguard.live_execution import LiveExecutionAdapter
from spotguard.market import SpotMarketSnapshot, scaled_synthetic_klines
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from tests.helpers import config_dict

OWNER="123456789"
MARKET=SpotMarketSnapshot("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("5"),Decimal("0.001"),"TRADING",1)

def configured(root: Path, *, enabled=False, scheduled="paper"):
 d=config_dict(root); d["live"]["enabled"]=enabled; d["scheduled_proposal_mode"]=scheduled
 p=root/"config.json"; p.write_text(json.dumps(d)); return load_settings(p)

class LiveSafetyTests(unittest.TestCase):
 def test_canonical_limits_and_default_mode(self):
  with tempfile.TemporaryDirectory() as d:
   s=configured(Path(d)); self.assertEqual(s.risk.max_quote_per_trade,Decimal("100")); self.assertEqual(s.paper.max_open_positions,10); self.assertEqual(s.paper.max_quote_per_entry_usdt,Decimal("100"))
   self.assertEqual(s.live.max_quote_per_entry_usdt,Decimal("100")); self.assertEqual(s.live.max_active_tranches,10); self.assertEqual(s.live.max_economic_positions,5)
   self.assertEqual(s.live.max_open_exposure_usdt,Decimal("500")); self.assertEqual(s.live.min_free_reserve_usdt,Decimal("8"))
   self.assertEqual(s.live.max_risk_per_position_usdt,Decimal("2")); self.assertEqual(s.live.max_aggregate_risk_usdt,Decimal("4"))
   self.assertEqual(s.live.daily_realized_loss_cap_usdt,Decimal("5")); self.assertEqual(s.live.max_successful_entries_per_utc_day,10); self.assertEqual(s.scheduled_proposal_mode,"paper")

 def test_status_distinguishes_default_order_size_from_entry_maxima(self):
  with tempfile.TemporaryDirectory() as d:
   status=SpotGuard(configured(Path(d))).status()["limits"]
   self.assertEqual(status["default_order_size_usdt"],"6.0")
   self.assertEqual(status["paper"]["max_quote_per_entry_usdt"],"100")
   self.assertEqual(status["live"]["max_quote_per_entry_usdt"],"100")
   self.assertEqual(status["paper"]["max_open_exposure_usdt"],"500")
   self.assertEqual(status["live"]["max_open_exposure_usdt"],"500")

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_paper_hundred_is_accepted_and_above_hundred_is_rejected(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   proposal=SpotGuard(configured(Path(d))).create_manual_buy_proposal("BTC",Decimal("100"))["proposal"]
   self.assertEqual(proposal["mode"],"paper"); self.assertEqual(proposal["quote_amount"],"100")
  with tempfile.TemporaryDirectory() as d:
   with self.assertRaisesRegex(Exception,"exceeds configured maximum 100 USDT"):
    SpotGuard(configured(Path(d))).create_manual_buy_proposal("BTC",Decimal("100.01"))

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_live_hundred_amount_fixture_is_proposal_only_and_above_is_rejected(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True)); service.live_status=Mock(return_value={"execution_ready":True})
   proposal=service.create_manual_buy_proposal("BTC",Decimal("100"),live=True)["proposal"]
   self.assertEqual(proposal["mode"],"live"); self.assertEqual(proposal["status"],"PENDING"); self.assertIsNone(proposal["execution_status"])
   with self.assertRaises(SecurityError): service.execute_paper(proposal["id"],"invalid")
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True))
   with self.assertRaisesRegex(Exception,"exceeds configured maximum 100 USDT"):
    service.create_manual_buy_proposal("BTC",Decimal("100.01"),live=True)

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_paper_six_accepted_over_hundred_and_min_notional_rejected_without_raise(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   self.assertEqual(service.create_manual_buy_proposal("BTC",Decimal("6"))["proposal"]["mode"],"paper")
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   with self.assertRaises(Exception): service.create_manual_buy_proposal("BTC",Decimal("100.01"))
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   with patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET.__class__("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("7"),Decimal("0.001"),"TRADING",1)):
    with self.assertRaisesRegex(Exception,"minimum notional"): service.create_manual_buy_proposal("BTC",Decimal("6"))

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_paper_stays_paper_when_live_enabled(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   self.assertEqual(service.create_manual_buy_proposal("BTC",Decimal("6"))["proposal"]["mode"],"paper")

 def test_live_buy_disabled_disarmed_not_ready(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   with self.assertRaisesRegex(SecurityError,"disabled"): service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   with self.assertRaisesRegex(SecurityError,"armed"): service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_ready_fixture_live_buy_is_proposal_only(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True))
   service.live_status=Mock(return_value={"execution_ready":True})
   proposal=service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)["proposal"]
   self.assertEqual(proposal["mode"],"live"); self.assertEqual(proposal["order_type"],"LIMIT"); self.assertEqual(proposal["status"],"PENDING")
   self.assertIsNone(proposal["execution_status"])
   with self.assertRaises(SecurityError): service.execute_paper(proposal["id"],"invalid")

 def test_adapter_is_immutable_and_only_builds_protected_otoco(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); adapter=service.live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","product":"SPOT","side":"BUY","order_type":"LIMIT","symbol":"BTCUSDT","quote_amount":"6","quantity":"0.06","entry_limit_price":"100","stop_reference":"98","take_profit_reference":"104"}}
   frozen=adapter.freeze(proposal)
   with self.assertRaises(TypeError): frozen["symbol"]="ETHUSDT"
   request=adapter.protected_request(proposal)
   self.assertEqual(request["toolName"],"spot.orderList.place.otoco")
   self.assertEqual(request["arguments"]["workingSide"],"BUY")
   self.assertEqual(request["arguments"]["pendingSide"],"SELL")
   self.assertEqual(request["arguments"]["pendingBelowStopPrice"],"98")
   self.assertEqual(request["arguments"]["pendingAbovePrice"],"104")
   with self.assertRaisesRegex(SecurityError,"exact approved"): adapter.execute(proposal)
   for tool in ("futures.order","margin.order","wallet.withdraw","generic.tool_execute"):
    with self.assertRaises(SecurityError): LiveExecutionAdapter.validate_tool("binance-mcp-server",tool)
   ids=adapter.client_ids("p-1234567890ab"); self.assertEqual(ids["order_list_client_id"],"sgl-1234567890ab")
   with self.assertRaisesRegex(SecurityError,"malformed"): adapter.validate_write_response({"status":"ok"})
   with self.assertRaisesRegex(SecurityError,"ambiguous"): adapter.validate_write_response({"orderListId":1,"listStatusType":"UNKNOWN"})
   with self.assertRaisesRegex(SecurityError,"must not be retried"): adapter.reconcile("p-1234567890ab")

 def test_duplicate_exposure_and_fourth_position_guards(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":1,"active_tranches":1,"free_usdt":"94","realized_pnl":"0"}), patch.object(service.ledger,"list_paper_positions",return_value=[{"symbol":"BTCUSDT","quote_spent":"6","risk_amount":"0.10"}]), patch.object(service.ledger,"has_active_paper_buy",return_value=True):
    with self.assertRaisesRegex(Exception,"pending or processing"): service._ensure_paper_entry_available("BTCUSDT",Decimal("6"))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":4,"active_tranches":4,"free_usdt":"200"}), patch.object(service.ledger,"list_paper_positions",return_value=[{"symbol":"BTCUSDT","quote_spent":"250","risk_amount":"1"},{"symbol":"ETHUSDT","quote_spent":"200","risk_amount":"1"}]):
    with self.assertRaisesRegex(Exception,"500 USDT"): service._ensure_paper_entry_available("SOLUSDT",Decimal("100"))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":5,"active_tranches":10,"free_usdt":"100"}), patch.object(service.ledger,"list_paper_positions",return_value=[]):
    with self.assertRaisesRegex(Exception,"active PAPER tranche limit"): service._ensure_paper_entry_available("SOLUSDT",Decimal("6"))

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_only_one_pending_live_proposal(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True)); service.live_status=Mock(return_value={"execution_ready":True})
   service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
   with self.assertRaisesRegex(Exception,"maximum active"): service.create_manual_buy_proposal("ETH",Decimal("6"),live=True)

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 def test_scheduled_live_mode_creates_proposal_not_execution(self, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True,scheduled="live")); service.live_status=Mock(return_value={"execution_ready":True})
   candidate=service.scan(symbols=["BTCUSDT"],synthetic=True)["results"][0]["candidate"]
   result=service.create_proposal(candidate["id"],Decimal(str(candidate["price"]))*Decimal("0.999"),Decimal(str(candidate["price"])),Decimal("6"),"scheduled")
   self.assertEqual(result["proposal"]["mode"],"live"); self.assertEqual(result["proposal"]["status"],"PENDING"); self.assertIsNone(result["proposal"]["execution_status"])

 def test_paper_text_approval_cannot_cross_to_live(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-000000000001","status":"PENDING","mode":"live","expires_at":"2999-01-01T00:00:00Z","canonical":{"mode":"live","approval_owner_id":OWNER,"approval_chat_id":OWNER}}
   with patch.object(service.ledger,"get_proposal",return_value=proposal):
    with self.assertRaisesRegex(SecurityError,"paper-only"): service._validate_paper_confirmation(proposal["id"],"CODE",OWNER,OWNER)

 def test_non_tty_admin_refuses(self):
  from spotguard.cli import main
  with tempfile.TemporaryDirectory() as d:
   path=Path(d)/"config.json"; path.write_text(json.dumps(config_dict(Path(d))))
   with patch("sys.stdin.isatty",return_value=False):
    self.assertEqual(main(["--config",str(path),"--json","scheduled-mode","set","live","--owner-id",OWNER]),2)
   self.assertEqual(json.loads(path.read_text())["scheduled_proposal_mode"],"paper")

if __name__=="__main__": unittest.main()
