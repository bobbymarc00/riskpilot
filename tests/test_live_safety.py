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
   service_status=SpotGuard(configured(Path(d))).status()
   self.assertEqual(service_status["version"],"1.0.1")
   status=service_status["limits"]
   self.assertEqual(status["default_order_size_usdt"],"6.0")
   self.assertEqual(status["paper"]["max_quote_per_entry_usdt"],"100")
   self.assertEqual(status["live"]["max_quote_per_entry_usdt"],"100")
   self.assertEqual(status["paper"]["max_open_exposure_usdt"],"500")
   self.assertEqual(status["live"]["max_open_exposure_usdt"],"500")

 def test_readiness_does_not_treat_profile_configuration_as_scope_proof(self):
  with tempfile.TemporaryDirectory() as d:
   readiness=LiveExecutionAdapter(configured(Path(d),enabled=True)).readiness(connected=True,armed=True,symbol_flags_verified=True)
   self.assertTrue(hasattr(readiness,"execution_profile_configured"))
   self.assertFalse(readiness.account_read_verified)
   self.assertFalse(readiness.spot_trade_scope_verified)
   self.assertFalse(readiness.write_schema_verified)
   self.assertFalse(readiness.execution_ready)

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
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True)); service.live_status=Mock(return_value={"execution_ready":True}); service._validate_live_entry_limits=Mock(return_value={})
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
   service.live_status=Mock(return_value={"execution_ready":True}); service._validate_live_entry_limits=Mock(return_value={})
   proposal=service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)["proposal"]
   self.assertEqual(proposal["mode"],"live"); self.assertEqual(proposal["order_type"],"LIMIT"); self.assertEqual(proposal["status"],"PENDING")
   self.assertIsNone(proposal["execution_status"])
   with self.assertRaises(SecurityError): service.execute_paper(proposal["id"],"invalid")

 def test_adapter_is_immutable_and_only_builds_protected_otoco(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); adapter=service.live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","product":"SPOT","side":"BUY","order_type":"LIMIT","symbol":"BTCUSDT","quote_amount":"6","quantity":"0.06","pending_quantity":"0.06","entry_limit_price":"100","stop_reference":"98","take_profit_reference":"104","price_tick_size":"0.01"}}
   frozen=adapter.freeze(proposal)
   with self.assertRaises(TypeError): frozen["symbol"]="ETHUSDT"
   request=adapter.protected_request(proposal)
   self.assertEqual(request["toolName"],"spot.orderListOtoco")
   self.assertEqual(request["arguments"]["workingSide"],"BUY")
   self.assertEqual(request["arguments"]["pendingSide"],"SELL")
   self.assertEqual(request["arguments"]["pendingBelowStopPrice"],98.0)
   self.assertEqual(request["arguments"]["pendingBelowPrice"],97.9)
   self.assertEqual(request["arguments"]["pendingAbovePrice"],104.0)
   with self.assertRaisesRegex(SecurityError,"exact approved"): adapter.execute(proposal)
   for tool in ("futures.order","margin.order","wallet.withdraw","generic.tool_execute"):
    with self.assertRaises(SecurityError): LiveExecutionAdapter.validate_tool("binance-mcp-server",tool)
   ids=adapter.client_ids("p-1234567890ab"); self.assertEqual(ids["order_list_client_id"],"sgl-1234567890ab")
   with self.assertRaisesRegex(SecurityError,"malformed"): adapter.validate_write_response({"status":"ok"})
   with self.assertRaisesRegex(SecurityError,"ambiguous"): adapter.validate_write_response({"orderListId":1,"listStatusType":"UNKNOWN"})
   with self.assertRaisesRegex(SecurityError,"must not be retried"): adapter.reconcile("p-1234567890ab")

 def test_adapter_only_allows_exact_live_market_close_shape(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-close","product":"SPOT","side":"SELL","order_type":"MARKET","symbol":"BTCUSDT","quote_asset":"USDT","quote_amount":"0","quantity":"0.06","market_step_size":"0.001"}}
   request=adapter.protected_request(proposal)
   self.assertEqual(request["toolName"],"spot.newOrder")
   self.assertEqual(request["arguments"],{"symbol":"BTCUSDT","side":"SELL","type":"MARKET","quantity":0.06,"newClientOrderId":"sgc-1234567890ab"})
   self.assertEqual(adapter.validate_write_response({"orderId":7,"status":"FILLED"},close=True),"FILLED")
   for status in ("NEW", "PARTIALLY_FILLED"):
    with self.assertRaisesRegex(SecurityError,"reconciliation required"):
     adapter.validate_write_response({"orderId":7,"status":status},close=True)
   malformed={**proposal,"canonical":{**proposal["canonical"],"side":"BUY"}}
   with self.assertRaises(SecurityError): adapter.protected_request(malformed)

 def test_partial_exit_requires_immutable_cancel_sell_rearm_terms(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-partial-exit","product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","symbol":"BTCUSDT","quote_amount":"0","cancel_order_id":11,"order_list_id":12,"sell_quantity":"0.02","remaining_quantity":"0.04","market_step_size":"0.001","price_tick_size":"0.01","stop_reference":"98","take_profit_reference":"104"}}
   self.assertEqual(adapter.freeze(proposal)["source"],"manual-live-partial-exit")
   bad={**proposal,"canonical":{**proposal["canonical"],"side":"BUY"}}
   with self.assertRaises(SecurityError): adapter.freeze(bad)

 def test_duplicate_exposure_and_fourth_position_guards(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d)))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":1,"active_tranches":1,"free_usdt":"94","realized_pnl":"0"}), patch.object(service.ledger,"list_paper_positions",return_value=[{"symbol":"BTCUSDT","quote_spent":"6","risk_amount":"0.10"}]), patch.object(service.ledger,"has_active_paper_buy",return_value=True):
    with self.assertRaisesRegex(Exception,"pending or processing"): service._ensure_paper_entry_available("BTCUSDT",Decimal("6"))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":4,"active_tranches":4,"free_usdt":"200"}), patch.object(service.ledger,"list_paper_positions",return_value=[{"symbol":"BTCUSDT","quote_spent":"250","risk_amount":"1"},{"symbol":"ETHUSDT","quote_spent":"200","risk_amount":"1"}]):
    with self.assertRaisesRegex(Exception,"500 USDT"): service._ensure_paper_entry_available("SOLUSDT",Decimal("100"))
   with patch.object(service.ledger,"paper_balance",return_value={"open_positions":5,"active_tranches":10,"free_usdt":"100"}), patch.object(service.ledger,"list_paper_positions",return_value=[]):
    with self.assertRaisesRegex(Exception,"active PAPER tranche limit"): service._ensure_paper_entry_available("SOLUSDT",Decimal("6"))

 def test_live_snapshot_enforces_reserve_and_records_projection(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"108","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   service.live_executor.read_spot_trades=Mock(return_value=[])
   projection=service._validate_live_entry_limits("BTCUSDT",Decimal("100"),Decimal("1"),Decimal("2"),Decimal("100"))
   self.assertEqual(projection["projected_free_balance"],"8")
   self.assertEqual(projection["projected_total_exposure"],"100")
   self.assertTrue(projection["live_risk_snapshot_verified"])
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"107.99","locked":"0"}]})
   with self.assertRaisesRegex(Exception,"reserve"):
    service._validate_live_entry_limits("BTCUSDT",Decimal("100"),Decimal("1"),Decimal("2"),Decimal("100"))

 def test_live_snapshot_refuses_unverified_sale_or_existing_position(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"1000","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   service.live_executor.read_spot_trades=Mock(return_value=[{"isBuyer":False,"qty":"1","quoteQty":"100","time":1}])
   with self.assertRaisesRegex(SecurityError,"realized loss"):
    service._validate_live_entry_limits("BTCUSDT",Decimal("6"),Decimal("0.06"),Decimal("1"),Decimal("100"))

 def test_live_snapshot_refuses_unprotected_base_balance(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"1000","locked":"0"},{"asset":"BTC","free":"0.1","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   with self.assertRaisesRegex(SecurityError,"not fully protected"):
    service._validate_live_entry_limits("BTCUSDT",Decimal("6"),Decimal("0.06"),Decimal("1"),Decimal("100"))

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_only_one_pending_live_proposal(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True)); service.live_status=Mock(return_value={"execution_ready":True}); service._validate_live_entry_limits=Mock(return_value={})
   service.create_manual_buy_proposal("BTC",Decimal("6"),live=True)
   with self.assertRaisesRegex(Exception,"maximum active"): service.create_manual_buy_proposal("ETH",Decimal("6"),live=True)

 @patch("spotguard.service.fetch_klines",return_value=scaled_synthetic_klines(100))
 @patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET)
 def test_scheduled_live_mode_creates_proposal_not_execution(self, market, klines):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True,scheduled="live")); service.live_status=Mock(return_value={"execution_ready":True}); service._validate_live_entry_limits=Mock(return_value={"live_risk_snapshot_verified":True,"projected_free_balance":"994","projected_total_exposure":"6","projected_aggregate_risk":"1","live_entries_today":0,"live_daily_realized_loss":"0","live_weekly_realized_loss":"0","active_live_tranches":0,"active_live_economic_positions":0})
   candidate=service.scan(symbols=["BTCUSDT"],synthetic=True)["results"][0]["candidate"]
   result=service.create_proposal(candidate["id"],Decimal(str(candidate["price"]))*Decimal("0.999"),Decimal(str(candidate["price"])),Decimal("6"),"scheduled")
   self.assertEqual(result["proposal"]["mode"],"live"); self.assertEqual(result["proposal"]["status"],"PENDING"); self.assertIsNone(result["proposal"]["execution_status"])
   canonical=result["proposal"]["canonical"]
   self.assertIn("quantity",canonical); self.assertIn("pending_quantity",canonical)
   self.assertTrue(canonical["live_risk_snapshot_verified"])
   service._validate_live_entry_limits.assert_called_once()
   token=service.signer.approval_token(result["proposal"]["canonical_json"])
   claim=service.claim(result["proposal"]["id"],token,OWNER,OWNER)
   self.assertEqual(service._validate_live_entry_limits.call_count,2)
   service.live_arm.status=Mock(return_value=Mock(armed=True))
   service.live_executor.execute=Mock(return_value={"orderListId":42,"listStatusType":"RESPONSE"})
   executed=service.execute_live(result["proposal"]["id"],claim["lease"])
   self.assertEqual(executed["status"],"EXECUTED")
   self.assertEqual(service._validate_live_entry_limits.call_count,3)

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

 def test_live_partial_exit_creates_dormant_rounded_proposal(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True)); service.live_arm.status=Mock(return_value=Mock(armed=True)); service.live_status=Mock(return_value={"execution_ready":True})
   orders=[
    {"symbol":"BTCUSDT","orderListId":12,"orderId":11,"side":"SELL","status":"NEW","type":"STOP_LOSS_LIMIT","origQty":"0.20","stopPrice":"98"},
    {"symbol":"BTCUSDT","orderListId":12,"orderId":13,"side":"SELL","status":"NEW","type":"TAKE_PROFIT_LIMIT","origQty":"0.20","stopPrice":"104","price":"104"},
   ]
   service.live_executor.read_open_spot_orders=Mock(return_value=orders)
   exchange={"market_step_size":"0.001","price_tick_size":"0.01","min_notional":"5"}
   with patch("spotguard.service.validate_spot_symbol",return_value=exchange), patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET):
    proposal=service.create_live_partial_exit_proposal("BTC",Decimal("65"))["proposal"]
   canonical=proposal["canonical"]
   self.assertEqual(proposal["status"],"PENDING")
   self.assertEqual(canonical["source"],"manual-live-partial-exit")
   self.assertEqual(canonical["sell_quantity"],"0.13")
   self.assertEqual(canonical["remaining_quantity"],"0.07")
   self.assertEqual(canonical["cancel_order_id"],11)
   self.assertEqual(canonical["stop_reference"],"98")
   self.assertEqual(canonical["take_profit_reference"],"104")

 def test_partial_exit_executes_only_cancel_sell_then_rearm_in_mock(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-partial-exit","product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","symbol":"BTCUSDT","quote_amount":"0","cancel_order_id":11,"order_list_id":12,"sell_quantity":"0.02","remaining_quantity":"0.04","market_step_size":"0.001","price_tick_size":"0.01","stop_reference":"98","take_profit_reference":"104"}}
   replies=[
    {"structuredContent":{"orderId":11,"status":"CANCELED"}},
    {"structuredContent":{"orderId":14,"status":"FILLED"}},
    {"structuredContent":{"orderListId":15,"listStatusType":"RESPONSE","orderReports":[{"clientOrderId":"sgt-1234567890ab","status":"NEW"},{"clientOrderId":"sgs-1234567890ab","status":"NEW"}]}},
   ]
   adapter._direct_mcp_result=Mock(side_effect=replies)
   adapter.read_open_spot_orders=Mock(return_value=[])
   adapter.read_spot_account=Mock(return_value={"balances":[{"asset":"BTC","free":"0.06"}]})
   result=adapter.execute_partial_exit(proposal)
   self.assertEqual(result["orderId"],14)
   self.assertEqual(adapter._direct_mcp_result.call_count,3)
   cancel,sell,rearm=[call.args[0] for call in adapter._direct_mcp_result.call_args_list]
   self.assertEqual(cancel["toolName"],"spot.deleteOrder")
   self.assertEqual(sell["arguments"]["quantity"],0.02)
   self.assertEqual(rearm["toolName"],"spot.orderListOco")
   self.assertEqual(rearm["arguments"]["quantity"],0.04)
   self.assertEqual(rearm["arguments"]["belowPrice"],97.9)

 def test_partial_exit_partial_fill_does_not_rearm(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-partial-exit","product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","symbol":"BTCUSDT","quote_amount":"0","cancel_order_id":11,"order_list_id":12,"sell_quantity":"0.02","remaining_quantity":"0.04","market_step_size":"0.001","price_tick_size":"0.01","stop_reference":"98","take_profit_reference":"104"}}
   replies=[{"structuredContent":{"orderId":11,"status":"CANCELED"}},{"structuredContent":{"orderId":14,"status":"PARTIALLY_FILLED"}}]
   adapter._direct_mcp_result=Mock(side_effect=replies)
   adapter.read_open_spot_orders=Mock(return_value=[])
   adapter.read_spot_account=Mock(return_value={"balances":[{"asset":"BTC","free":"0.06"}]})
   with self.assertRaisesRegex(SecurityError,"reconciliation required"):
    adapter.execute_partial_exit(proposal)
   self.assertEqual(adapter._direct_mcp_result.call_count,2)

 def test_partial_exit_does_not_sell_when_cancel_is_ambiguous(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-partial-exit","product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","symbol":"BTCUSDT","quote_amount":"0","cancel_order_id":11,"order_list_id":12,"sell_quantity":"0.02","remaining_quantity":"0.04","market_step_size":"0.001","price_tick_size":"0.01","stop_reference":"98","take_profit_reference":"104"}}
   adapter._direct_mcp_result=Mock(return_value={"structuredContent":{"orderId":11,"status":"NEW"}})
   adapter.read_open_spot_orders=Mock(return_value=[{"symbol":"BTCUSDT","orderListId":12}])
   with self.assertRaises(SecurityError): adapter.execute_partial_exit(proposal)
   self.assertEqual(adapter._direct_mcp_result.call_count,1)

 def test_partial_exit_accepts_ambiguous_cancel_only_after_no_oco_reconciliation(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=SpotGuard(configured(Path(d),enabled=True)).live_executor
   proposal={"id":"p-1234567890ab","mode":"live","canonical":{"mode":"live","source":"manual-live-partial-exit","product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","symbol":"BTCUSDT","quote_asset":"USDT","quote_amount":"0","cancel_order_id":11,"order_list_id":12,"sell_quantity":"0.02","remaining_quantity":"0","market_step_size":"0.001","price_tick_size":"0.01","stop_reference":"98","take_profit_reference":"104"}}
   replies=[{"structuredContent":{"orderId":11,"status":"NEW"}},{"structuredContent":{"orderId":14,"status":"FILLED"}}]
   adapter._direct_mcp_result=Mock(side_effect=replies)
   adapter.read_open_spot_orders=Mock(return_value=[])
   adapter.read_spot_account=Mock(return_value={"balances":[{"asset":"BTC","free":"0.02"}]})
   result=adapter.execute_partial_exit(proposal)
   self.assertEqual(result["orderId"],14)
   self.assertEqual(adapter._direct_mcp_result.call_count,2)

if __name__=="__main__": unittest.main()
