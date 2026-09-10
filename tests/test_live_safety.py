from __future__ import annotations
import json, tempfile, unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from spotguard.config import load_settings
from spotguard.live_execution import LiveExecutionAdapter, MCPTransportError
from spotguard.market import SpotMarketSnapshot, scaled_synthetic_klines
from spotguard.security import SecurityError
from spotguard.service import SpotGuard
from spotguard.presentation import render
from spotguard.telegram import proposal_message
from spotguard.market import classify_spot_base_balance
from spotguard.util import isoformat, utcnow
from tests.helpers import config_dict

OWNER="123456789"
MARKET=SpotMarketSnapshot("BTCUSDT",Decimal("99.9"),Decimal("100"),Decimal("100"),Decimal("5"),Decimal("0.001"),"TRADING",1)

def configured(root: Path, *, enabled=False, scheduled="paper"):
 d=config_dict(root); d["live"]["enabled"]=enabled; d["scheduled_proposal_mode"]=scheduled
 p=root/"config.json"; p.write_text(json.dumps(d)); return load_settings(p)

class LiveSafetyTests(unittest.TestCase):
 def test_exchange_dust_predicate_uses_filters_not_fixed_floor(self):
  filters={"status":"TRADING","market_step_size":"0.001","market_min_qty":"0.001","min_notional":"5"}
  dust=classify_spot_base_balance(Decimal("0.000942"),filters,Decimal("100"))
  self.assertEqual(dust["classification"],"EXCHANGE_DUST")
  self.assertFalse(dust["tradable"])
  tradable=classify_spot_base_balance(Decimal("0.001"),filters,Decimal("6000"))
  self.assertTrue(tradable["tradable"])
  low_notional=classify_spot_base_balance(Decimal("0.001"),filters,Decimal("100"))
  self.assertEqual(low_notional["classification"],"EXCHANGE_DUST")
  with self.assertRaises(Exception):
   classify_spot_base_balance(Decimal("0.001"),filters,None)

 def test_closed_incomplete_epoch_is_terminal_and_new_epoch_is_clean(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-legacy","status":"active","profile_fingerprint":profile,"started_at":"2026-01-01T00:00:00Z"})
   service.ledger.add_event("live.risk_fill",None,{"epoch_id":"le-legacy","side":"BUY","quantity":"1","price":"100","fee_quote":"0","executed_at":"2026-01-01T00:00:00Z"})
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-legacy","status":"CLOSED_INCOMPLETE","realized_pnl_verified":False,"accounting_complete":False,"profile_fingerprint":profile})
   self.assertEqual(service._effective_live_risk_epoch()["status"],"CLOSED_INCOMPLETE")
   self.assertFalse(service._epoch_blocks_new_live_entry(service._effective_live_risk_epoch()))
   fresh=service._ensure_live_risk_epoch([{"asset":"USDT","free":"33","locked":"0"}])
   self.assertNotEqual(fresh["epoch_id"],"le-legacy")
   self.assertTrue(service._live_session_accounting()[0])

 def test_unresolved_epoch_states_block_new_epoch(self):
  for state in ("active","RECONCILE","PNL_INCOMPLETE"):
   with self.subTest(state=state), tempfile.TemporaryDirectory() as d:
    service=SpotGuard(configured(Path(d),enabled=True))
    service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-block","status":state,"profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
    self.assertTrue(service._epoch_blocks_new_live_entry(service._effective_live_risk_epoch()))
    if state == "active":
     self.assertEqual(service._ensure_live_risk_epoch([])["epoch_id"],"le-block")
    else:
      with self.assertRaisesRegex(SecurityError,"requires operator reconciliation"):
       service._ensure_live_risk_epoch([])

 def test_empty_live_epoch_aborts_locally_and_allows_fresh_epoch(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-empty","status":"RECONCILE","profile_fingerprint":profile,"started_at":"2026-01-01T00:00:00Z","reconcile_reason":"legacy validation"})
   service.live_executor.read_spot_account=Mock(side_effect=AssertionError("empty epoch must not read account"))
   service.live_executor.read_open_spot_orders=Mock(side_effect=AssertionError("empty epoch must not read orders"))
   result=service.finalize_live_risk_epoch(operator_confirmed=True,empty_only=True)
   self.assertEqual(result["status"],"ABORTED_EMPTY")
   self.assertFalse(result["realized_pnl_verified"])
   self.assertEqual(service.ledger.events_by_kind("live.risk_fill"),[])
   fresh=service._ensure_live_risk_epoch([])
   self.assertNotEqual(fresh["epoch_id"],"le-empty")

 def test_empty_finalization_refuses_activity_evidence(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-active","status":"ACTIVE","profile_fingerprint":profile,"started_at":"2026-01-01T00:00:00Z"})
   service.ledger.add_event("execution.completed", "proposal-1", {"epoch_id":"le-active","status":"EXECUTED"})
   with self.assertRaisesRegex(SecurityError,"touched-symbol evidence"):
    service.finalize_live_risk_epoch(operator_confirmed=True,empty_only=True)

 def test_legacy_finalization_uses_one_verified_price_for_notional_dust(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-price-dust","status":"RECONCILE","started_at":"2026-01-01T00:00:00Z","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   service.ledger.active_proposals_status=Mock(return_value=[])
   service.ledger.list_proposals=Mock(return_value=[{"mode":"live","symbol":"BTCUSDT","created_at":"2026-01-02T00:00:00Z"}])
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"BTC","free":"0.001883","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   filters={"status":"TRADING","market_step_size":"0.001","market_min_qty":"0.001","min_notional":"5"}
   snapshot=SimpleNamespace(symbol="BTCUSDT",bid=Decimal("100"),observed_at_ms=123)
   with patch("spotguard.service.validate_spot_symbol",return_value=filters), patch("spotguard.service.fetch_spot_snapshot",return_value=snapshot) as price:
    result=service.finalize_live_risk_epoch(operator_confirmed=True)
   self.assertEqual(result["status"],"CLOSED_INCOMPLETE")
   price.assert_called_once()
   dust=service.ledger.latest_event("live.risk_epoch")["dust_balances"][0]
   self.assertEqual(dust["classification"],"EXCHANGE_DUST")
   self.assertEqual(dust["reference_price_used"],"100")

 def test_legacy_finalization_refuses_tradable_residual_at_verified_price(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-price-tradable","status":"RECONCILE","started_at":"2026-01-01T00:00:00Z","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   service.ledger.active_proposals_status=Mock(return_value=[])
   service.ledger.list_proposals=Mock(return_value=[{"mode":"live","symbol":"BTCUSDT","created_at":"2026-01-02T00:00:00Z"}])
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"BTC","free":"0.001883","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   with patch("spotguard.service.validate_spot_symbol",return_value={"status":"TRADING","market_step_size":"0.001","market_min_qty":"0.001","min_notional":"5"}), patch("spotguard.service.fetch_spot_snapshot",return_value=SimpleNamespace(symbol="BTCUSDT",bid=Decimal("6000"),observed_at_ms=123)):
    with self.assertRaisesRegex(SecurityError,"meaningful LIVE base balance"):
     service.finalize_live_risk_epoch(operator_confirmed=True)

 def test_proposal_presentation_uses_canonical_mode_and_order_type(self):
  canonical={"mode":"live","product":"SPOT","side":"BUY","order_type":"LIMIT","symbol":"SOLUSDT",
             "quote_amount":"6","entry_reference":"102.02000000","stop_reference":"101.35000000",
             "take_profit_reference":"103.36000000","reward_risk":"2"}
  proposal={**canonical,"id":"p-live-1","status":"PENDING","expires_at":"2099-01-01T00:00:00Z","canonical":canonical}
  cli=render({"proposal":proposal},"en","live-buy")
  self.assertIn("LIVE proposal created",cli)
  self.assertNotIn("PAPER",cli)
  self.assertNotIn("simulated fill",cli)
  text, buttons=proposal_message(proposal,"token")
  self.assertIn("Spot LIMIT BUY SOLUSDT",text)
  self.assertNotIn("Spot MARKET BUY",text)
  self.assertIn("Entry reference: 102.02\nStop reference: 101.35\nTarget reference: 103.36",text)
  self.assertNotIn("Eny reference",text)
  self.assertEqual([button["label"] for button in buttons], ["APPROVE LIVE","REJECT LIVE"])
  self.assertEqual(canonical["order_type"],"LIMIT")

 def test_paper_proposal_presentation_remains_paper(self):
  proposal={"mode":"paper","id":"p-paper-1"}
  self.assertIn("PAPER proposal created",render({"proposal":proposal},"en","paper-buy"))

 def test_execution_discovery_reuses_wrapper_and_trade_catalog(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   wrapper={"name":"tool_execute","inputSchema":{"type":"object","properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}},"required":["toolName"],"additionalProperties":False}}
   row={"name":"spot.orderTest","inputSchema":{"type":"object","properties":{},"required":[],"additionalProperties":False}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[wrapper]}, {"structuredContent":{"tools":[row]}}])
   adapter.execution_discovery()
   adapter.execution_discovery()
   self.assertEqual(adapter._direct_mcp_jsonrpc.call_count, 2)

 def test_read_capability_discovery_reports_no_candidates_without_invocation(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter.execution_discovery=Mock(return_value=({},{}))
   result=adapter.discover_live_execution_read_capabilities(operator_confirmed=True)
   self.assertEqual(result["individual_order_status_count"],0)
   self.assertEqual(result["order_list_status_count"],0)
   self.assertEqual(result["trade_fill_history_count"],0)
   self.assertFalse(result["writes_invoked"])

 def test_read_capability_discovery_accepts_one_unique_candidate_per_class(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   output={"type":"object","properties":{"status":{"type":"string"},"executedQty":{"type":"string"},"cumulativeQuoteQty":{"type":"string"}}}
   catalog={
    "spot.getOrder":{"name":"spot.getOrder","description":"Read-only Spot individual order status and detail","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"orderId":{"type":"integer"}},"required":["symbol","orderId"],"additionalProperties":False},"outputSchema":output},
    "spot.getOrderList":{"name":"spot.getOrderList","description":"Read-only Spot OCO/OTOCO order list status","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"orderListId":{"type":"integer"}},"required":["symbol","orderListId"],"additionalProperties":False},"outputSchema":{"type":"object","properties":{"listStatusType":{"type":"string"}}}},
    "spot.myTrades":{"name":"spot.myTrades","description":"Read-only Spot account trade history and fills","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"orderId":{"type":"integer"}},"required":["symbol"],"additionalProperties":False},"outputSchema":{"type":"array","items":{"type":"object","properties":{"price":{"type":"string"},"qty":{"type":"string"},"quoteQty":{"type":"string"},"commission":{"type":"string"},"commissionAsset":{"type":"string"}}}}},
   }
   adapter.execution_discovery=Mock(return_value=({},catalog))
   result=adapter.discover_live_execution_read_capabilities(operator_confirmed=True)
   self.assertEqual(result["individual_order_status"]["unique_candidate"],"spot.getOrder")
   self.assertEqual(result["order_list_status"]["unique_candidate"],"spot.getOrderList")
   self.assertEqual(result["trade_fill_history"]["unique_candidate"],"spot.myTrades")

 def test_read_capability_discovery_marks_ambiguous_and_rejects_writes_or_non_spot(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   def row(name, description, props):
    return {"name":name,"description":description,"inputSchema":{"type":"object","properties":props,"required":list(props),"additionalProperties":False}}
   catalog={
    "spot.getOrderA":row("spot.getOrderA","Read-only Spot order status",{"symbol":{"type":"string"},"orderId":{"type":"integer"}}),
    "spot.getOrderB":row("spot.getOrderB","Read-only Spot order status",{"symbol":{"type":"string"},"orderId":{"type":"integer"}}),
    "spot.newOrder":row("spot.newOrder","Create new Spot order",{"symbol":{"type":"string"},"orderId":{"type":"integer"}}),
    "futures.getOrder":row("futures.getOrder","Read-only futures order status",{"symbol":{"type":"string"},"orderId":{"type":"integer"}}),
   }
   adapter.execution_discovery=Mock(return_value=({},catalog))
   result=adapter.discover_live_execution_read_capabilities(operator_confirmed=True)
   self.assertEqual(result["individual_order_status"]["selection"],"AMBIGUOUS")
   rejected={row["toolName"]:row for row in result["individual_order_status"]["candidates"] if not row["accepted"]}
   self.assertIn("spot.newOrder",rejected)
   self.assertIn("futures.getOrder",rejected)

 def test_read_capability_discovery_keeps_order_list_distinct_and_requires_account_history_semantics(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   row={"name":"spot.getOpenOrders","description":"Read-only Spot open orders","inputSchema":{"type":"object","properties":{"symbol":{"type":"string"},"orderListId":{"type":"integer"}},"required":["symbol"],"additionalProperties":False}}
   adapter.execution_discovery=Mock(return_value=({}, {row["name"]:row}))
   result=adapter.discover_live_execution_read_capabilities(operator_confirmed=True)
   self.assertEqual(result["order_list_status_count"],0)
   self.assertEqual(result["trade_fill_history_count"],0)
   self.assertIn("order_list_semantics_not_proven",result["order_list_status"]["candidates"][0]["reason"])

 def test_unreconcilable_async_execution_requires_zero_read_capabilities_and_preserves_state(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-async0000001","mode":"live","status":"EXECUTED","execution_status":"EXEC_STARTED"}
   evidence={"proposal_id":proposal["id"],"epoch_id":"le-async","phase":"SUBMITTED",
             "entry":{"status":"NEW","order_id":101,"order_list_id":202,"executed_qty":"0"},
             "protection":[{"orderId":203,"status":"PENDING_NEW"},{"orderId":204,"status":"PENDING_NEW"}]}
   service.ledger.get_proposal=Mock(return_value=proposal)
   service.ledger.events_by_kind=Mock(side_effect=lambda kind: [evidence] if kind == "live.execution_evidence" else [])
   service.ledger.latest_event=Mock(return_value={"individual_order_status_count":0,"order_list_status_count":0,"trade_fill_history_count":0})
   service._effective_live_risk_epoch=Mock(return_value={"epoch_id":"le-async","status":"ACTIVE"})
   service.ledger.mark_live_execution_unreconcilable=Mock(return_value={"status":"RECONCILE"})
   result=service.mark_live_execution_unreconcilable(proposal["id"],operator_confirmed=True)
   self.assertEqual(result["reason"],"ASYNC_FILL_PROVENANCE_UNRECOVERABLE_UPSTREAM_CAPABILITY")
   service.ledger.mark_live_execution_unreconcilable.assert_called_once()

 def test_unreconcilable_async_execution_refuses_when_read_capability_exists_or_fill_is_verified(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-async0000002","mode":"live","status":"EXECUTED","execution_status":"EXEC_STARTED"}
   evidence={"proposal_id":proposal["id"],"epoch_id":"le-async","phase":"SUBMITTED",
             "entry":{"status":"NEW","order_id":101,"order_list_id":202,"executed_qty":"0"},
             "protection":[{"orderId":203}]}
   service.ledger.get_proposal=Mock(return_value=proposal)
   service.ledger.events_by_kind=Mock(side_effect=lambda kind: [evidence] if kind == "live.execution_evidence" else [])
   service.ledger.latest_event=Mock(return_value={"individual_order_status_count":1,"order_list_status_count":0,"trade_fill_history_count":0})
   with self.assertRaisesRegex(SecurityError,"read capability"):
    service.mark_live_execution_unreconcilable(proposal["id"],operator_confirmed=True)
   evidence["phase"]="FILLED"; evidence["entry"]["status"]="FILLED"; evidence["entry"]["executed_qty"]="1"
   service.ledger.latest_event=Mock(return_value={"individual_order_status_count":0,"order_list_status_count":0,"trade_fill_history_count":0})
   with self.assertRaisesRegex(SecurityError,"verified FILLED"):
    service.mark_live_execution_unreconcilable(proposal["id"],operator_confirmed=True)

 def test_prepare_live_session_blocked_makes_zero_remote_calls(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor._activate_rate_limit(reason="BINANCE_-1003")
   service.live_executor._direct_mcp_jsonrpc=Mock(side_effect=AssertionError("blocked preflight must not call MCP"))
   result=service.prepare_live_session("BTCUSDT",operator_confirmed=True)
   self.assertFalse(result["execution_ready"])
   self.assertEqual(result["blockers"],["binance_rate_limit_blocked"])
   service.live_executor._direct_mcp_jsonrpc.assert_not_called()

 def test_arm_consumes_prepared_session_without_mcp_calls(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.settings=replace(service.settings,codex=replace(service.settings.codex,mcp_server="binance-execution"))
   service.live_executor.settings=service.settings
   service.live_executor.execution_profile_fingerprint=Mock(return_value="profile-fp")
   service.live_executor._direct_mcp_jsonrpc=Mock(side_effect=AssertionError("arm must not call MCP"))
   common={"result":"verified","delegated_operation":"spot.orderTest","profile_fingerprint":"profile-fp","schema_fingerprint":"schema-fp","expires_at":"2999-01-01T00:00:00+00:00"}
   service.ledger.add_event("live.trade_permission_attestation",None,dict(common))
   service.ledger.add_event("live.decimal_transport_attestation",None,{**common,"wire_mode":"fixed-point-json-number","classification":"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG","scope":"bounded_decimal_domain","tested_fields":["quantity","price"],"minimum_verified_fractional_number":"0.001"})
   service.ledger.add_event("live.session_prepared",None,{"result":"prepared","prepared_at":"2999-01-01T00:00:00+00:00","expires_at":"2999-01-01T00:05:00+00:00","target_symbol":"BTCUSDT","backend":"binance-execution","profile_fingerprint":"profile-fp","schema_fingerprint":"schema-fp","account_read_verified":True,"open_orders_read_verified":True,"spot_trade_scope_verified":True,"write_tool_discovered":True,"write_schema_verified":True,"decimal_transport_verified":True,"decimal_transport_mode":"bounded","minimum_verified_fractional_number":"0.001","protective_order_capability_verified":True,"symbol_exchange_flags_verified":True,"live_limits_valid":True,"live_enabled":True,"rate_limit_status":"CLEAR","final_blockers":["live_armed"]})
   service.live_arm.arm=Mock(return_value=SimpleNamespace(armed=True,expires_at="2999-01-01T00:30:00+00:00",reason="armed"))
   result=service.arm_live(30)
   self.assertTrue(result["armed"])
   service.live_executor._direct_mcp_jsonrpc.assert_not_called()
   self.assertEqual(service.ledger.latest_event("live.session_prepared_consumed")["prepared_at"],"2999-01-01T00:00:00+00:00")

 def test_arm_without_prepared_session_fails_closed_without_mcp(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor._direct_mcp_jsonrpc=Mock(side_effect=AssertionError("arm must not call MCP"))
   with self.assertRaisesRegex(SecurityError,"prepare-live-session"):
    service.arm_live(30)
   service.live_executor._direct_mcp_jsonrpc.assert_not_called()

 def test_reconcile_recovery_ticket_is_exit_only_and_local_arm_is_scoped(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-reconcile","status":"RECONCILE","profile_fingerprint":profile})
   proof={"result":"verified","delegated_operation":"spot.orderTest","profile_fingerprint":profile,"schema_fingerprint":"schema-fp","expires_at":"2999-01-01T00:00:00+00:00"}
   service.ledger.add_event("live.trade_permission_attestation",None,proof)
   service.ledger.add_event("live.decimal_transport_attestation",None,{**proof,"wire_mode":"fixed-point-json-number","classification":"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG","scope":"bounded_decimal_domain","tested_fields":["quantity","price"],"minimum_verified_fractional_number":"0.001"})
   service.live_executor._valid_permission_attestation=Mock(return_value=True)
   service.live_executor._valid_decimal_transport_attestation=Mock(return_value=True)
   legs=[{"symbol":"BTCUSDT","orderListId":10,"orderId":11,"side":"SELL","status":"NEW","type":"STOP_LOSS_LIMIT","origQty":"0.5","stopPrice":"99"},
         {"symbol":"BTCUSDT","orderListId":10,"orderId":12,"side":"SELL","status":"NEW","type":"TAKE_PROFIT_LIMIT","origQty":"0.5","stopPrice":"105"}]
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"SOL","free":"0","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=legs)
   with patch("spotguard.service.validate_spot_symbol",return_value={"price_tick_size":"0.01"}):
    prepared=service.prepare_live_recovery_session("BTCUSDT",operator_confirmed=True)
   self.assertEqual(prepared["scope"],"EXIT_ONLY")
   service.live_arm.arm=Mock(return_value=SimpleNamespace(armed=True,expires_at="2099-01-01T00:15:00+00:00",reason="armed",scope="EXIT_ONLY"))
   armed=service.arm_live_recovery(15)
   self.assertEqual(armed["scope"],"EXIT_ONLY")
   self.assertEqual(service.live_executor.read_spot_account.call_count,1)
   self.assertEqual(service.live_executor.read_open_spot_orders.call_count,1)

 def test_exit_only_scope_rejects_live_buy_before_transport(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_arm.status=Mock(return_value=SimpleNamespace(armed=True,scope="EXIT_ONLY"))
   with self.assertRaisesRegex(SecurityError,"EXIT_ONLY"):
    service.create_manual_buy_proposal("BTCUSDT",Decimal("6"),live=True)

 def test_exit_only_approval_uses_recovery_gate_not_normal_readiness(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-exit","status":"RECONCILE","profile_fingerprint":profile})
   prepared={"result":"prepared","scope":"EXIT_ONLY","prepared_at":"2999-01-01T00:00:00+00:00","expires_at":"2999-01-01T00:05:00+00:00","target_symbol":"BTCUSDT","epoch_id":"le-exit","profile_fingerprint":profile,"order_list_id":10,"protected_order_ids":[11,12],"protected_quantity":"0.5","permission_proof_valid":True,"decimal_proof_valid":True,"schema_fingerprint":"schema-fp"}
   service.ledger.add_event("live.recovery_session_prepared",None,prepared)
   service.live_arm.status=Mock(return_value=SimpleNamespace(armed=True,scope="EXIT_ONLY",binding={"prepared_at":prepared["prepared_at"],"target_symbol":"BTCUSDT"}))
   proof={"result":"verified","delegated_operation":"spot.orderTest","profile_fingerprint":profile,"schema_fingerprint":"schema-fp","expires_at":"2999-01-01T00:00:00+00:00"}
   service.ledger.add_event("live.trade_permission_attestation",None,proof)
   service.ledger.add_event("live.decimal_transport_attestation",None,{**proof,"wire_mode":"fixed-point-json-number","classification":"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG","scope":"bounded_decimal_domain","tested_fields":["quantity","price"],"minimum_verified_fractional_number":"0.001"})
   service.live_status=Mock(side_effect=AssertionError("recovery approval must not use normal readiness"))
   proposal={"canonical":{"symbol":"BTCUSDT","side":"SELL","order_type":"PARTIAL_EXIT","order_list_id":10,"protected_order_ids":[11,12],"sell_quantity":"0.058","percentage":"100","remaining_quantity":"0"}}
   service._validate_exit_only_approval(proposal,service.live_arm.status())

 def test_live_status_readiness_uses_backend_proofs_fail_closed(self):
  symbol_info={"symbol":"BTCUSDT","oto_allowed":True,"opo_allowed":True,"oco_allowed":True,
               "price_tick_size":"0.01","percent_price_filter":True,"max_num_orders":5,
               "max_num_algo_orders":5,"max_num_order_lists":5}
  evidence={"spot_trade_scope_verified":True,"write_tool_discovered":True,"write_schema_verified":True}
  cases=(("all proofs valid",evidence,True,False),("invalid schema",{**evidence,"write_schema_verified":False},True,False),
         ("account read failed",evidence,False,False),("not armed",evidence,True,False))
  for name, probe, account_ok, expected in cases:
   with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
    service=SpotGuard(configured(Path(d),enabled=True))
    service.settings=replace(service.settings,
      codex=replace(service.settings.codex,mcp_server="binance-execution"),
      live=replace(service.settings.live,protective_orders_available=True))
    service.live_executor.settings=service.settings
    service.agent_os.status=Mock(return_value={"authenticated":True,"mcp_configured":True})
    service.live_arm.status=Mock(return_value=Mock(armed=(name != "not armed")))
    service.live_executor.read_open_spot_orders=Mock(return_value=[])
    service.live_executor.read_spot_account=(Mock(return_value={"account_type":"SPOT","can_trade":True})
      if account_ok else Mock(side_effect=SecurityError("account read failed")))
    service.live_executor._valid_permission_attestation=Mock(return_value=(name == "all proofs valid"))
    service.live_executor.readiness_probe=Mock(return_value=probe)
    with patch("spotguard.service.validate_spot_symbol",return_value=symbol_info):
     result=service.live_status(check_symbols=True,symbols=["BTCUSDT"])
    self.assertEqual(result["execution_ready"],expected)
    self.assertFalse(result["decimal_transport_verified"])
    self.assertIn("REMOTE_MCP_DECIMAL_CONTRACT_BLOCKER",result["blockers"])
    if name == "invalid schema": self.assertIn("write_schema_verified",result["blockers"])

 def test_explicit_order_test_attestation_uses_only_fixed_target(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   test_schema={"type":"object","required":["symbol","side","type"],
                "properties":{"symbol":{"type":"string"},"side":{"type":"string"},
                              "type":{"type":"string"},"quantity":{"type":"number"},
                              "quoteOrderQty":{"type":"number"},"price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
            "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[
    {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":test_schema}]}},
    {"structuredContent":{}},
   ])
   result=adapter.attest_spot_trade_permission(
    "BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"SUCCESS")
   invocation=adapter._direct_mcp_jsonrpc.call_args.args[1]
   self.assertEqual(invocation["name"],"tool_execute")
   self.assertEqual(invocation["arguments"]["toolName"],"spot.orderTest")
   delegated=invocation["arguments"]["arguments"]
   self.assertTrue(delegated)
   self.assertEqual(delegated["symbol"],"BTCUSDT")
   self.assertEqual(delegated["side"],"BUY")
   self.assertEqual(delegated["type"],"MARKET")
   self.assertEqual(delegated["quoteOrderQty"],Decimal("5"))
   self.assertNotIn("quantity",delegated)
   self.assertNotIn("price",delegated)
   self.assertNotIn("timeInForce",delegated)
   self.assertNotRegex(str(delegated),r"(?i)(token|secret|signature|authorization|api[_-]?key)")
   self.assertNotIn("spot.newOrder", str(adapter._direct_mcp_jsonrpc.call_args_list))

 def test_binance_decimal_wire_encoding_never_uses_exponents(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   for value in ("0.00007","0.00000001","0.001","1.00000000","123.450000","70000.01"):
    wire=adapter._canonical_wire_json({"quantity":Decimal(value),"price":Decimal(value)})
    self.assertNotRegex(wire,r":-?[0-9.]+[eE][+-]?[0-9]+")
    self.assertIn(f'"quantity":{value}',wire)
    self.assertIn(f'"price":{value}',wire)

 def test_empty_order_test_payload_fails_before_remote_transport(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":{
    "symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
    "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},"price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,"properties":{
    "toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}}])
   adapter._validate_test_order_arguments=Mock(return_value="spot.orderTest payload omits a schema-required field")
   result=adapter.attest_spot_trade_permission("BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"PAYLOAD_VALIDATION_FAILURE")
   self.assertEqual(result["stage"],"BEFORE_TOOL_EXECUTE")
   self.assertEqual(adapter._direct_mcp_jsonrpc.call_count,2)

 def test_remote_transport_error_keeps_sanitized_diagnostics(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":{
    "symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
    "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},"price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,"properties":{
    "toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    MCPTransportError("remote error message token=SECRET",stage="REMOTE_JSONRPC",error_code=-2015)])
   result=adapter.attest_spot_trade_permission("BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"TRANSPORT_FAILURE")
   self.assertEqual(result["stage"],"REMOTE_JSONRPC")
   self.assertEqual(result["error_code"],-2015)
   self.assertNotIn("SECRET",result["detail"])

 def test_order_test_authorization_failure_does_not_attest(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":{
    "symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},"quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},"price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,"properties":{
    "toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    {"isError":True,"content":[{"text":"permission denied"}]}])
   result=adapter.attest_spot_trade_permission("BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"AUTHORIZATION_FAILURE")

 def test_benign_mcp_content_is_not_classified_as_failure(self):
  for response in ({"isError":False,"content":[{"type":"text","text":"{}"}]},
                   {"content":[{"type":"text","text":"{}"}]}):
   with self.subTest(response=response), tempfile.TemporaryDirectory() as d:
    adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
    schema={"type":"object","required":["symbol","side","type"],"properties":
            {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
             "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},
             "price":{"type":"number"},"timeInForce":{"type":"string"}}}
    wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
             "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
    adapter._direct_mcp_jsonrpc=Mock(side_effect=[
     {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
     {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}}, response])
    result=adapter.attest_spot_trade_permission("BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
    self.assertEqual(result["classification"],"SUCCESS")

 def test_mcp_error_content_is_classified_without_leaking_benign_success(self):
  cases=(("-2015 Invalid API-key, IP, or permissions","AUTHORIZATION_FAILURE"),
         ("-1100 Illegal characters found in parameter 'quantity'","PAYLOAD_VALIDATION_FAILURE"))
  for message, expected in cases:
   with self.subTest(message=message), tempfile.TemporaryDirectory() as d:
    adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
    schema={"type":"object","required":["symbol","side","type"],"properties":
             {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
             "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},"price":{"type":"number"},
             "timeInForce":{"type":"string"}}}
    wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
             "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
    adapter._direct_mcp_jsonrpc=Mock(side_effect=[
     {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
     {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
     {"isError":True,"content":[{"type":"text","text":message}]}])
    result=adapter.attest_spot_trade_permission("BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
    self.assertEqual(result["classification"],expected)
    self.assertIn(message, result["detail"])

 def test_successful_attestation_is_persisted_and_expiry_is_fail_closed(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.read_spot_account=Mock(return_value={"account_type":"SPOT","can_trade":True})
   service.live_executor.attest_spot_trade_permission=Mock(return_value={
    "classification":"SUCCESS","schema_fingerprint":"schema-fp","delegated_tool":"spot.orderTest"})
   service.live_executor.execution_profile_fingerprint=Mock(return_value="profile-fp")
   with patch("spotguard.service.validate_spot_symbol",return_value={"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}), patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET):
    result=service.verify_live_trade_permission(operator_confirmed=True)
   self.assertEqual(result["classification"],"SUCCESS")
   proof=service.ledger.latest_event("live.trade_permission_attestation")
   self.assertEqual(proof["delegated_operation"],"spot.orderTest")
   self.assertTrue(service.live_executor._valid_permission_attestation(proof,"schema-fp"))
   expired=dict(proof); expired["expires_at"]="2000-01-01T00:00:00+00:00"
   self.assertFalse(service.live_executor._valid_permission_attestation(expired,"schema-fp"))

 def test_rate_limit_circuit_blocks_readiness_without_transport_calls(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter._activate_rate_limit(reason="BINANCE_-1003")
   transport=Mock()
   adapter._direct_mcp_jsonrpc=transport
   status=adapter.rate_limit_status()
   self.assertEqual(status["status"],"BLOCKED")
   self.assertEqual(status["reason"],"BINANCE_-1003")
   proof=adapter.verify_readiness(connected=True,symbol_flags_verified=True)
   self.assertFalse(proof["account_read_verified"])
   self.assertFalse(proof["open_orders_read_verified"])
   self.assertFalse(proof["spot_trade_scope_verified"])
   self.assertEqual(transport.call_count,0)

 def test_rate_limit_http_codes_and_unknown_expiry_fail_closed(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   self.assertTrue(adapter._is_rate_limit_error(None,"HTTP 429"))
   self.assertTrue(adapter._is_rate_limit_error(None,"HTTP 418 IP banned"))
   self.assertFalse(adapter._is_rate_limit_error(-1100,"invalid quantity"))
   adapter._activate_rate_limit(reason="HTTP_429")
   self.assertEqual(adapter.rate_limit_status()["blocked_until"],"UNKNOWN")
   adapter._activate_rate_limit(reason="HTTP_429",blocked_until="2000-01-01T00:00:00+00:00")
   self.assertEqual(adapter.rate_limit_status()["status"],"CLEAR")
   adapter._activate_rate_limit(reason="HTTP_429",blocked_until="2999-01-01T00:00:00+00:00")
   self.assertEqual(adapter.rate_limit_status()["status"],"BLOCKED")

 def test_decimal_transport_guard_cannot_be_overridden(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   readiness=adapter.readiness(connected=True,armed=True,symbol_flags_verified=True,
    account_read_verified=True,open_orders_read_verified=True,spot_trade_scope_verified=True,
    write_tool_discovered=True,write_schema_verified=True)
   self.assertFalse(readiness.decimal_transport_verified)
   self.assertFalse(readiness.execution_ready)

 def test_bounded_decimal_domain_accepts_floor_and_rejects_smaller_values_without_rounding(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   for value in ("0.001", "0.001000", "0.005", "1.25", "78219.58"):
    adapter.validate_remote_decimal_domain({"toolName":"spot.newOrder","arguments":{"quantity":Decimal(value)}})
   for field, value in (("quantity","0.000999"),("quantity","0.00007000"),("stopPrice","0.00007000")):
    with self.subTest(field=field,value=value):
     request={"toolName":"spot.orderListOco","arguments":{field:Decimal(value)}}
     with self.assertRaisesRegex(SecurityError,"below remotely verified decimal floor 0.001"):
      adapter.validate_remote_decimal_domain(request)
     self.assertEqual(request["arguments"][field],Decimal(value))

 def test_unsafe_decimal_is_rejected_before_remote_result_transport(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter._direct_mcp_jsonrpc=Mock()
   with self.assertRaisesRegex(SecurityError,"field quantity is below remotely verified decimal floor 0.001"):
    adapter._direct_mcp_result({"toolName":"spot.newOrder","arguments":{"quantity":Decimal("0.00007")}})
   adapter._direct_mcp_jsonrpc.assert_not_called()

 def test_fractional_decimal_transport_attestation_uses_fixed_json_numbers(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":
           {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
            "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},
            "price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
            "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[
    {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    {"isError":False,"content":[{"type":"text","text":"{}"}]}])
   result=adapter.attest_decimal_transport(
    "BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"SUCCESS")
   payload=adapter._direct_mcp_jsonrpc.call_args.args[1]["arguments"]["arguments"]
   self.assertIsInstance(payload["quantity"],Decimal)
   self.assertIsInstance(payload["price"],Decimal)
   self.assertGreater(payload["quantity"],0)
   wire=adapter._canonical_wire_json(payload)
   self.assertRegex(wire,r'"quantity":[0-9]+\.[0-9]+')
   self.assertRegex(wire,r'"price":[0-9]+\.[0-9]+')
   self.assertNotRegex(wire,r'(?i)"(?:quantity|price)":"|"(?:quantity|price)":-?[0-9.]+e')
   self.assertEqual(json.loads(wire,parse_float=Decimal)["quantity"],payload["quantity"])
   self.assertEqual(result["wire_mode"],"fixed-point-json-number")

 def test_decimal_attestation_persists_and_readiness_validates_it(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.attest_decimal_transport=Mock(return_value={
    "classification":"SUCCESS","schema_fingerprint":"decimal-schema",
    "delegated_tool":"spot.orderTest",
    "payload":{"quantity":Decimal("0.001"),"price":Decimal("100.01")}})
   service.live_executor.execution_profile_fingerprint=Mock(return_value="profile-fp")
   with patch("spotguard.service.validate_spot_symbol",return_value={"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}), \
        patch("spotguard.service.fetch_spot_snapshot",return_value=MARKET):
    result=service.verify_live_decimal_transport(operator_confirmed=True)
   self.assertEqual(result["classification"],"SUCCESS")
   proof=service.ledger.latest_event("live.decimal_transport_attestation")
   self.assertTrue(service.live_executor._valid_decimal_transport_attestation(proof,"decimal-schema"))
   expired=dict(proof); expired["expires_at"]="2000-01-01T00:00:00+00:00"
   self.assertFalse(service.live_executor._valid_decimal_transport_attestation(expired,"decimal-schema"))
   wrong_profile=dict(proof); wrong_profile["profile_fingerprint"]="other"
   self.assertFalse(service.live_executor._valid_decimal_transport_attestation(wrong_profile,"decimal-schema"))
   missing_fields=dict(proof); missing_fields["tested_fields"]=["quantity"]
   self.assertFalse(service.live_executor._valid_decimal_transport_attestation(missing_fields,"decimal-schema"))

 def test_valid_bounded_decimal_proof_enables_only_decimal_readiness_proof(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter.read_spot_account=Mock(return_value={"account_type":"SPOT","can_trade":True})
   adapter.read_open_spot_orders=Mock(return_value=[])
   adapter.readiness_probe=Mock(return_value={"write_tool_discovered":True,"write_schema_verified":True,
    "test_order_schema_fingerprint":"schema-fp","reasons":[]})
   adapter.execution_profile_fingerprint=Mock(return_value="profile-fp")
   proof={"result":"verified","delegated_operation":"spot.orderTest","profile_fingerprint":"profile-fp",
          "schema_fingerprint":"schema-fp","wire_mode":"fixed-point-json-number",
          "tested_fields":["quantity","price"],"minimum_verified_fractional_number":"0.001",
          "classification":"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG","scope":"bounded_decimal_domain",
          "expires_at":"2999-01-01T00:00:00+00:00"}
   result=adapter.verify_readiness(connected=True,symbol_flags_verified=True,
    permission_attestation=None,decimal_transport_attestation=proof)
   self.assertTrue(result["decimal_transport_verified"])
   self.assertEqual(result["minimum_verified_fractional_number"],"0.001")

 def test_decimal_attestation_remote_1100_is_blocker_without_proof(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":
           {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
            "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},
            "price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
            "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[
    {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    {"isError":True,"content":[{"type":"text","text":"-1100 Illegal characters found in parameter 'quantity'"}]}])
   result=adapter.attest_decimal_transport(
    "BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"REMOTE_MCP_DECIMAL_CONTRACT_BLOCKER")

 def test_decimal_diagnostic_uses_larger_fractional_quantity_without_proof(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":
           {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
            "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},
            "price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
            "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[
    {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    {"isError":False,"content":[{"type":"text","text":"{}"}]}])
   result=adapter.diagnose_decimal_transport(
    "BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.00001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"REMOTE_MCP_SMALL_DECIMAL_SERIALIZATION_BUG")
   self.assertEqual(result["remote_result"],"SUCCESS")
   self.assertGreaterEqual(Decimal(result["requested_quantity"]),Decimal("0.001"))
   self.assertNotIn("live.decimal_transport_attestation", str(result))

 def test_decimal_diagnostic_quantity_error_is_general_contract_bug(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   schema={"type":"object","required":["symbol","side","type"],"properties":
           {"symbol":{"type":"string"},"side":{"type":"string"},"type":{"type":"string"},
            "quantity":{"type":"number"},"quoteOrderQty":{"type":"number"},
            "price":{"type":"number"},"timeInForce":{"type":"string"}}}
   wrapper={"type":"object","required":["toolName"],"additionalProperties":False,
            "properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}}}
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[
    {"tools":[{"name":"tool_execute","inputSchema":wrapper}]},
    {"structuredContent":{"tools":[{"name":"spot.orderTest","inputSchema":schema}]}},
    {"isError":True,"content":[{"type":"text","text":"-1100 Illegal characters found in parameter 'quantity'"}]}])
   result=adapter.diagnose_decimal_transport(
    "BTCUSDT", {"price_tick_size":"0.01","market_step_size":"0.00001","min_notional":"5"}, MARKET)
   self.assertEqual(result["classification"],"REMOTE_MCP_GENERAL_DECIMAL_CONTRACT_BUG")

 def test_live_status_does_not_refresh_decimal_attestation(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.attest_decimal_transport=Mock(side_effect=AssertionError("must not attest from status"))
   service.agent_os.status=Mock(return_value={"authenticated":False,"mcp_configured":False})
   service.live_arm.status=Mock(return_value=Mock(armed=False,expires_at=None))
   result=service.live_status(check_symbols=False)
   self.assertFalse(result["decimal_transport_verified"])
   service.live_executor.attest_decimal_transport.assert_not_called()

 def test_readiness_probe_requires_exact_spot_tools_and_schema(self):
  with tempfile.TemporaryDirectory() as d:
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   def row(name, missing=None):
    fields=adapter._REQUIRED_WRITE_SCHEMAS.get(name,{"symbol"})
    props={key:{"type":"number" if key in {"quantity","price","stopPrice","workingPrice","workingQuantity","pendingQuantity","pendingAbovePrice","pendingAboveStopPrice","pendingBelowPrice","pendingBelowStopPrice"} else "integer" if key == "orderId" else "string"}
           for key in fields if key != missing}
    enum_values={"side":["SELL"],"type":["MARKET"],"aboveType":["TAKE_PROFIT_LIMIT"],"belowType":["STOP_LOSS_LIMIT"],
                 "pendingSide":["SELL"],"pendingAboveType":["TAKE_PROFIT_LIMIT"],"pendingBelowType":["STOP_LOSS_LIMIT"],
                 "workingSide":["BUY"],"workingType":["LIMIT"]}
    for key, values in enum_values.items():
     if key in props: props[key]["enum"]=values
    return {"name":name,"inputSchema":{"type":"object","properties":props,"required":[],"additionalProperties":False}}
   wrapper={"name":"tool_execute","inputSchema":{"type":"object","properties":{"toolName":{"type":"string"},"arguments":{"type":"object"}},"required":["toolName"],"additionalProperties":False}}
   def mcp(value): return {"structuredContent":value}
   names=list(adapter._REQUIRED_WRITE_SCHEMAS)
   trade=[row(name) for name in names]
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[wrapper]},mcp({"tools":trade[:2],"nextCursor":"page2"}),mcp({"tools":trade[2:]}),mcp({"tools":[]})])
   result=adapter.readiness_probe()
   self.assertTrue(result["write_tool_discovered"]); self.assertTrue(result["write_schema_verified"])
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[wrapper]},mcp({"tools":[row(name) for name in names[:3]]})])
   self.assertFalse(adapter.readiness_probe()["write_tool_discovered"])
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[wrapper]},mcp({"tools":[row(name, "quantity") if name == "spot.orderListOco" else row(name) for name in names]}),mcp({"tools":[]})])
   self.assertFalse(adapter.readiness_probe()["write_schema_verified"])
   adapter=LiveExecutionAdapter(configured(Path(d),enabled=True))
   adapter._direct_mcp_jsonrpc=Mock(side_effect=[{"tools":[wrapper]},mcp({"tools":[row("futures.order"),row("margin.order"),row("convert.order")]})])
   self.assertFalse(adapter.readiness_probe()["write_tool_discovered"])

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
   self.assertEqual(service_status["version"],"1.0.3")
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
   proposal["canonical"]["toolName"]="futures.order"
   self.assertEqual(adapter.protected_request(proposal)["toolName"],"spot.orderListOtoco")
   for delegated in ("futures.order","margin.order","convert.order","arbitrary.operation"):
    with self.assertRaises(SecurityError): adapter.validate_delegated_write_tool(delegated)
   for delegated in adapter._ALLOWED_WRITE_TOOLS:
    adapter.validate_delegated_write_tool(delegated)
   self.assertEqual(request["arguments"]["workingSide"],"BUY")
   self.assertEqual(request["arguments"]["pendingSide"],"SELL")
   self.assertEqual(request["arguments"]["pendingBelowStopPrice"],98.0)
   self.assertEqual(request["arguments"]["pendingBelowPrice"],Decimal("97.90"))
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
   self.assertEqual(request["arguments"],{"symbol":"BTCUSDT","side":"SELL","type":"MARKET","quantity":Decimal("0.06"),"newClientOrderId":"sgc-1234567890ab"})
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
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-test","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
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
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-test","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   service.ledger.add_event("live.risk_fill",None,{"epoch_id":"le-test","side":"SELL","quantity":"1","price":"100","fee_quote":"0","executed_at":"2026-01-01T00:00:00Z"})
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"1000","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   service.live_executor.read_spot_trades=Mock(return_value=[{"isBuyer":False,"qty":"1","quoteQty":"100","time":1}])
   with self.assertRaisesRegex(SecurityError,"RISKPILOT_SESSION_PNL_INCOMPLETE"):
    service._validate_live_entry_limits("BTCUSDT",Decimal("6"),Decimal("0.06"),Decimal("1"),Decimal("100"))

 def test_live_snapshot_refuses_unprotected_base_balance(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.live_executor.read_spot_account=Mock(return_value={"balances":[{"asset":"USDT","free":"1000","locked":"0"},{"asset":"BTC","free":"0.1","locked":"0"}]})
   service.live_executor.read_open_spot_orders=Mock(return_value=[])
   with self.assertRaisesRegex(SecurityError,"not fully protected"):
    service._validate_live_entry_limits("BTCUSDT",Decimal("6"),Decimal("0.06"),Decimal("1"),Decimal("100"))

 def test_live_session_accounting_uses_fifo_and_verified_fees(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   profile=service.live_executor.execution_profile_fingerprint()
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-fifo","status":"active","profile_fingerprint":profile})
   now=isoformat(utcnow())
   service.ledger.add_event("live.risk_fill", "proposal-buy-1", {"epoch_id":"le-fifo","proposal_id":"proposal-buy-1","order_id":"order-buy-1","side":"BUY","quantity":"1","price":"100","fee_quote":"0.10","executed_at":now})
   service.ledger.add_event("live.risk_fill", "proposal-buy-2", {"epoch_id":"le-fifo","proposal_id":"proposal-buy-2","order_id":"order-buy-2","side":"BUY","quantity":"1","price":"110","fee_quote":"0.10","executed_at":now})
   service.ledger.add_event("live.risk_fill", "proposal-sell-1", {"epoch_id":"le-fifo","proposal_id":"proposal-sell-1","order_id":"order-sell-1","side":"SELL","quantity":"1.5","price":"90","fee_quote":"0.20","executed_at":now})
   ok, daily, weekly, reason=service._live_session_accounting()
   self.assertTrue(ok)
   self.assertIsNone(reason)
   self.assertEqual(daily, Decimal("20.35"))
   self.assertEqual(weekly, Decimal("20.35"))

 def test_live_session_accounting_missing_fee_fails_closed(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-incomplete","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   service.ledger.add_event("live.risk_fill", "proposal-1", {"epoch_id":"le-incomplete","proposal_id":"proposal-1","order_id":"order-1","side":"BUY","quantity":"1","price":"100","executed_at":isoformat(utcnow())})
   ok, daily, weekly, reason=service._live_session_accounting()
   self.assertFalse(ok)
   self.assertEqual(reason,"RISKPILOT_SESSION_PNL_INCOMPLETE")
   self.assertEqual((daily,weekly),(Decimal("0"),Decimal("0")))

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
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-execute","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   service.live_executor.execute=Mock(return_value={"orderListId":42,"listStatusType":"RESPONSE","orderReports":[{"orderId":99,"side":"BUY","status":"FILLED","executedQty":"0.058","price":"101.97","fills":[{"price":"101.97","qty":"0.058","commission":"0.00001","commissionAsset":"BTC"}]}]})
   executed=service.execute_live(result["proposal"]["id"],claim["lease"])
   self.assertEqual(executed["status"],"EXECUTED")
   self.assertEqual(executed["accounting_status"],"VERIFIED")
   fills=service.ledger.events_by_kind("live.risk_fill")
   self.assertEqual(len(fills),1); self.assertEqual(fills[0]["delegated_order_id"],99)
   self.assertEqual(fills[0]["quantity"],"0.058"); self.assertEqual(fills[0]["price"],"101.97")
   evidence=service.ledger.events_by_kind("live.execution_evidence")
   self.assertEqual(len(evidence),1)
   self.assertEqual(evidence[0]["entry"]["order_id"],99)
   self.assertEqual(evidence[0]["entry"]["order_list_id"],42)
   self.assertEqual(service._validate_live_entry_limits.call_count,3)

 def test_live_fill_uses_verified_weighted_fills_and_preserves_fee_provenance(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-evidence","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   proposal={"id":"p-evidence0001","symbol":"BTCUSDT","side":"BUY"}
   response={"orderListId":246,"listStatusType":"RESPONSE","orderReports":[
    {"orderId":135,"side":"BUY","status":"FILLED","executedQty":"0.003","price":"100","fills":[
     {"price":"100","qty":"0.001","commission":"0.0001","commissionAsset":"USDT"},
     {"price":"102","qty":"0.002","commission":"0.0002","commissionAsset":"USDT"}]},
    {"orderId":136,"side":"SELL","status":"NEW","type":"STOP_LOSS_LIMIT"}]}
   fill=service._verified_live_fill(proposal,response,246)
   self.assertEqual(fill["delegated_order_id"],135)
   self.assertEqual(fill["order_list_id"],246)
   self.assertEqual(fill["price"],"101.3333333333333333333333333")
   self.assertEqual(fill["fee_amount"],"0.0003")
   self.assertEqual(fill["fee_asset"],"USDT")
   evidence=service._live_execution_evidence(proposal,response,246)
   service._persist_live_execution_evidence(evidence)
   self.assertEqual(service.ledger.events_by_kind("live.execution_evidence")[0]["entry"]["order_id"],135)

 def test_limit_price_without_fill_price_or_quote_is_not_accounting_price(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   service.ledger.add_event("live.risk_epoch",None,{"epoch_id":"le-price","status":"active","profile_fingerprint":service.live_executor.execution_profile_fingerprint()})
   proposal={"id":"p-price000001","symbol":"BTCUSDT","side":"BUY"}
   response={"orderListId":247,"orderReports":[{"orderId":136,"side":"BUY","status":"FILLED","executedQty":"0.003","price":"100"}]}
   self.assertIsNone(service._verified_live_fill(proposal,response,247))

 def test_async_live_reconciliation_filled_uses_one_read_and_creates_fill(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-async000001","mode":"live","status":"EXECUTING","execution_status":"EXEC_STARTED","symbol":"BTCUSDT","side":"BUY","execution_lease_hash":"lease-hash","canonical":{"symbol":"BTCUSDT"}}
   evidence={"proposal_id":proposal["id"],"epoch_id":"le-async","side":"BUY","phase":"SUBMITTED","entry":{"order_id":501,"order_list_id":601,"status":"NEW"},"protection":[{"order_id":502},{"order_id":503}]}
   response={"symbol":"BTCUSDT","orderId":501,"orderListId":601,"status":"FILLED","side":"BUY","executedQty":"0.06","cummulativeQuoteQty":"6.06","fills":[{"price":"101","qty":"0.03","commission":"0.001","commissionAsset":"USDT"},{"price":"101","qty":"0.03","commission":"0.001","commissionAsset":"USDT"}]}
   service.ledger.get_proposal=Mock(return_value=proposal)
   service.ledger.events_by_kind=Mock(side_effect=lambda kind: [evidence] if kind == "live.execution_evidence" else [])
   service.live_executor.read_spot_order_status=Mock(return_value=response)
   service._persist_live_execution_evidence=Mock()
   service.ledger.finish_execution=Mock(return_value={"status":"EXECUTED"})
   service._persist_live_risk_fill=Mock()
   service._live_session_accounting=Mock(return_value=(True,Decimal("0"),Decimal("0"),None))
   result=service.reconcile_live_execution(proposal["id"],operator_confirmed=True)
   self.assertEqual(result["status"],"EXECUTED")
   service.live_executor.read_spot_order_status.assert_called_once_with("BTCUSDT",501,601)
   service._persist_live_risk_fill.assert_called_once()
   self.assertEqual(service._persist_live_risk_fill.call_args.args[0]["quantity"],"0.06")

 def test_async_live_reconciliation_pending_stays_executing_without_fill(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-async000002","mode":"live","status":"EXECUTING","execution_status":"EXEC_STARTED","symbol":"BTCUSDT","side":"BUY","execution_lease_hash":"lease-hash","canonical":{"symbol":"BTCUSDT"}}
   evidence={"proposal_id":proposal["id"],"epoch_id":"le-async","side":"BUY","phase":"SUBMITTED","entry":{"order_id":501,"order_list_id":601,"status":"NEW"}}
   service.ledger.get_proposal=Mock(return_value=proposal)
   service.ledger.events_by_kind=Mock(side_effect=lambda kind: [evidence] if kind == "live.execution_evidence" else [])
   service.live_executor.read_spot_order_status=Mock(return_value={"symbol":"BTCUSDT","orderId":501,"orderListId":601,"status":"NEW","side":"BUY","executedQty":"0"})
   service._persist_live_execution_evidence=Mock()
   result=service.reconcile_live_execution(proposal["id"],operator_confirmed=True)
   self.assertEqual(result["status"],"EXECUTING")
   self.assertEqual(result["accounting_status"],"WAITING_FOR_FILL")

 def test_legacy_executed_exec_started_submission_is_reconcilable(self):
  with tempfile.TemporaryDirectory() as d:
   service=SpotGuard(configured(Path(d),enabled=True))
   proposal={"id":"p-async000003","mode":"live","status":"EXECUTED","symbol":"BTCUSDT","side":"BUY","execution_status":"EXEC_STARTED","execution_lease_hash":"lease-hash","canonical":{"symbol":"BTCUSDT"}}
   evidence={"proposal_id":proposal["id"],"epoch_id":"le-async","side":"BUY","phase":"SUBMITTED","entry":{"order_id":501,"order_list_id":601,"status":"NEW"},"protection":[]}
   response={"symbol":"BTCUSDT","orderId":501,"orderListId":601,"status":"FILLED","side":"BUY","executedQty":"0.06","cumulativeQuoteQty":"6.06","fills":[{"price":"101","qty":"0.06","commission":"0.001","commissionAsset":"USDT"}]}
   service.ledger.get_proposal=Mock(return_value=proposal)
   service.ledger.events_by_kind=Mock(side_effect=lambda kind: [evidence] if kind == "live.execution_evidence" else [])
   service.live_executor.read_spot_order_status=Mock(return_value=response)
   service._persist_live_execution_evidence=Mock()
   service.ledger.finish_execution=Mock(return_value={"status":"EXECUTED"})
   service._persist_live_risk_fill=Mock()
   service._live_session_accounting=Mock(return_value=(True,Decimal("0"),Decimal("0"),None))
   self.assertTrue(service._is_unresolved_live_execution(proposal))
   result=service.reconcile_live_execution(proposal["id"],operator_confirmed=True)
   self.assertEqual(result["status"],"EXECUTED")
   service.ledger.finish_execution.assert_called_once()
   self.assertTrue(service.ledger.finish_execution.call_args.kwargs["allow_legacy_unresolved"])

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
   self.assertEqual(sell["arguments"]["quantity"],Decimal("0.02"))
   self.assertEqual(rearm["toolName"],"spot.orderListOco")
   self.assertEqual(rearm["arguments"]["quantity"],Decimal("0.04"))
   self.assertEqual(rearm["arguments"]["belowPrice"],Decimal("97.90"))

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
