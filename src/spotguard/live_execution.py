from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .config import Settings
from .risk_policy.limits import absolute_entry_ceiling
from .security import SecurityError
from .util import canonical_json, decimal_value, isoformat, parse_time, utcnow


BINANCE_MCP_SERVER = "binance-execution"
FORBIDDEN_FAMILIES = ("futures", "margin", "convert", "wallet", "transfer", "payment", "withdraw", "borrow", "cancel")


class MCPTransportError(SecurityError):
    def __init__(self, message: str, *, stage: str, error_code: Any = None,
                 http_status: int | None = None) -> None:
        super().__init__(message)
        self.stage, self.error_code, self.http_status = stage, error_code, http_status


class RateLimitBlockedError(SecurityError):
    pass


@dataclass(frozen=True)
class LiveReadiness:
    binance_mcp_connected: bool
    execution_profile_configured: bool
    codex_login_reported: bool
    account_read_verified: bool
    open_orders_read_verified: bool
    spot_trade_scope_verified: bool
    write_tool_discovered: bool
    write_schema_verified: bool
    decimal_transport_verified: bool
    protective_order_capability_verified: bool
    symbol_exchange_flags_verified: bool
    live_limits_valid: bool
    live_enabled: bool
    live_armed: bool
    execution_ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "blockers": list(self.blockers)}


class LiveExecutionAdapter:
    """Fail-closed boundary for authenticated protected Binance Spot writes.

    The adapter submits only fixed allowlisted Spot request shapes through the
    dedicated Binance Agent OS/MCP execution profile. Every write is derived
    from immutable approved intent, validated before and after submission, and
    ambiguous outcomes are never automatically retried.
    """
    server = BINANCE_MCP_SERVER
    # Binance Agentic MCP uses one generic envelope. The delegated name is
    # therefore part of our local allowlist, not model-controlled text.
    tool_name = "tool_execute"
    delegated_tool_name = "spot.orderListOtoco"
    close_delegated_tool_name = "spot.newOrder"
    cancel_delegated_tool_name = "spot.deleteOrder"
    permission_test_tool_name = "spot.orderTest"
    _ALLOWED_WRITE_TOOLS = frozenset({"spot.orderListOtoco", "spot.orderListOco", "spot.newOrder", "spot.deleteOrder"})
    # Fixed read-only Telegram surface: balances plus active Spot TP/SL orders.
    _READ_REQUESTS = {
        "account": ("spot.getAccount", {"omitZeroBalances": True}),
        "open_orders": ("spot.getOpenOrders", {}),
    }
    _REQUIRED_WRITE_SCHEMAS = {
        "spot.orderListOtoco": {"symbol", "workingType", "workingSide", "workingPrice", "workingQuantity", "workingTimeInForce", "workingClientOrderId", "pendingSide", "pendingQuantity", "pendingAboveType", "pendingAbovePrice", "pendingAboveStopPrice", "pendingAboveTimeInForce", "pendingAboveClientOrderId", "pendingBelowType", "pendingBelowStopPrice", "pendingBelowPrice", "pendingBelowTimeInForce", "pendingBelowClientOrderId", "listClientOrderId"},
        "spot.orderListOco": {"symbol", "side", "quantity", "aboveType", "abovePrice", "aboveStopPrice", "aboveTimeInForce", "aboveClientOrderId", "belowType", "belowStopPrice", "belowPrice", "belowTimeInForce", "belowClientOrderId", "listClientOrderId"},
        "spot.newOrder": {"symbol", "side", "type", "quantity", "newClientOrderId"},
        "spot.deleteOrder": {"symbol", "orderId", "newClientOrderId"},
    }
    _TOOL_SEARCH_MAX_PAGES = 10
    _TOOL_SEARCH_MAX_ITEMS = 500
    _CATALOG_CACHE_TTL_SECONDS = 300
    _PERMISSION_ATTESTATION_TTL_SECONDS = 900
    _RATE_LIMIT_STATE_FILE = "binance-rate-limit-circuit.json"
    _DECIMAL_FIELDS = frozenset({
        "quantity", "quoteOrderQty", "price", "stopPrice", "icebergQty",
        "abovePrice", "belowPrice", "aboveStopPrice", "belowStopPrice",
        "pendingQuantity", "pendingAbovePrice", "pendingBelowPrice",
        "pendingAboveStopPrice", "pendingBelowStopPrice", "workingPrice",
        "workingQuantity",
    })
    _API_PERMISSION_FIELDS = {
        "enableReading", "enableSpotAndMarginTrading", "enableFutures", "enableMargin",
        "enableWithdrawals", "enableInternalTransfer", "permitsUniversalTransfer",
    }


    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._delegated_catalog_cache: tuple[float, dict[str, Mapping[str, Any]]] | None = None

    def freeze(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]:
        canonical = dict(proposal.get("canonical", {}))
        if proposal.get("mode") != "live" or canonical.get("mode") != "live":
            raise SecurityError("LiveExecutionAdapter accepts LIVE proposals only")
        is_close = canonical.get("source") == "manual-live-close"
        is_cancel = canonical.get("source") == "manual-live-cancel-protection"
        is_partial_exit = canonical.get("source") == "manual-live-partial-exit"
        is_set_protection = canonical.get("source") == "manual-live-set-protection"
        if is_set_protection:
            if (canonical.get("product"), canonical.get("side"), canonical.get("order_type")) != ("SPOT", "SELL", "OCO_PROTECTION"):
                raise SecurityError("live protection intent must be an exact Spot SELL OCO")
        elif is_partial_exit:
            if (canonical.get("product"), canonical.get("side"), canonical.get("order_type")) != ("SPOT", "SELL", "PARTIAL_EXIT"):
                raise SecurityError("live partial exit intent must be an exact Spot exit")
        elif is_cancel:
            if (canonical.get("product"), canonical.get("side"), canonical.get("order_type")) != ("SPOT", "CANCEL", "CANCEL_OCO"):
                raise SecurityError("live cancel intent must be an exact Spot OCO cancellation")
        elif is_close:
            if (canonical.get("product"), canonical.get("side"), canonical.get("order_type")) != ("SPOT", "SELL", "MARKET"):
                raise SecurityError("live close intent must be an exact Spot MARKET SELL")
        elif canonical.get("product") != "SPOT" or canonical.get("side") != "BUY" or canonical.get("order_type") not in {"LIMIT", "LIMIT_MAKER"}:
            raise SecurityError("live intent must be an exact protected Spot LIMIT BUY")
        if canonical.get("symbol") not in self.settings.market.symbols:
            raise SecurityError("live symbol is not configured")
        entry_ceiling = absolute_entry_ceiling(self.settings, "live")
        if (not is_close and not is_cancel and not is_partial_exit
                and not is_set_protection and entry_ceiling is not None
                and decimal_value(canonical.get("quote_amount"), "quote_amount")
                > entry_ceiling):
            raise SecurityError("live quote amount exceeds the configured per-entry limit")
        return MappingProxyType(canonical)

    def protected_request(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Build the only write shape RiskPilot permits, without sending it."""
        canonical = self.freeze(proposal)
        if canonical.get("source") == "manual-live-cancel-protection":
            order_id = canonical.get("cancel_order_id")
            order_list_id = canonical.get("order_list_id")
            if not isinstance(order_id, int) or not isinstance(order_list_id, int) or order_id <= 0 or order_list_id <= 0:
                raise SecurityError("live cancel order identifiers are invalid")
            ids = self.client_ids(str(proposal.get("id", "")))
            return {"toolName": self.cancel_delegated_tool_name, "arguments": {
                "symbol": canonical["symbol"], "orderId": order_id,
                "newClientOrderId": f"sgx-{ids['order_list_client_id'][4:]}",
            }}
        if canonical.get("source") == "manual-live-set-protection":
            quantity = decimal_value(canonical.get("quantity"), "quantity")
            step = decimal_value(canonical.get("market_step_size"), "market_step_size")
            tick = decimal_value(canonical.get("price_tick_size"), "price_tick_size")
            stop = decimal_value(canonical.get("stop_reference"), "stop_reference")
            target = decimal_value(canonical.get("take_profit_reference"), "take_profit_reference")
            if quantity <= 0 or step <= 0 or tick <= 0 or quantity % step or stop <= 0 or target <= stop or stop % tick or target % tick:
                raise SecurityError("live OCO protection terms are invalid")
            below = (stop * Decimal("0.999") // tick) * tick
            ids = self.client_ids(str(proposal.get("id", "")))
            return {"toolName": "spot.orderListOco", "arguments": {"symbol": canonical["symbol"], "side": "SELL", "quantity": quantity, "aboveType": "TAKE_PROFIT_LIMIT", "abovePrice": target, "aboveStopPrice": target, "aboveTimeInForce": "GTC", "aboveClientOrderId": ids["pending_above_client_order_id"], "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": stop, "belowPrice": below, "belowTimeInForce": "GTC", "belowClientOrderId": ids["pending_below_client_order_id"], "listClientOrderId": ids["order_list_client_id"]}}
        if canonical.get("source") == "manual-live-close":
            quantity = decimal_value(canonical.get("quantity"), "quantity")
            step = decimal_value(canonical.get("market_step_size"), "market_step_size")
            if quantity <= 0 or step <= 0 or quantity % step != 0:
                raise SecurityError("live close quantity is invalid or not exchange-aligned")
            ids = self.client_ids(str(proposal.get("id", "")))
            return {
                "toolName": self.close_delegated_tool_name,
                "arguments": {
                    "symbol": canonical["symbol"], "side": "SELL", "type": "MARKET",
                    "quantity": quantity, "newClientOrderId": f"sgc-{ids['order_list_client_id'][4:]}",
                },
            }
        required = ("quantity", "pending_quantity", "entry_limit_price", "stop_reference", "take_profit_reference", "price_tick_size")
        if any(key not in canonical for key in required):
            raise SecurityError("live proposal is missing required TP/SL protection fields")
        quantity = decimal_value(canonical["quantity"], "quantity")
        pending_quantity = decimal_value(canonical["pending_quantity"], "pending_quantity")
        entry = decimal_value(canonical["entry_limit_price"], "entry_limit_price")
        stop = decimal_value(canonical["stop_reference"], "stop_reference")
        target = decimal_value(canonical["take_profit_reference"], "take_profit_reference")
        tick = decimal_value(canonical["price_tick_size"], "price_tick_size")
        if quantity <= 0 or pending_quantity <= 0 or pending_quantity > quantity or tick <= 0 or stop <= 0 or target <= entry or stop >= entry:
            raise SecurityError("live TP/SL bracket is invalid")
        if any(price % tick != 0 for price in (entry, stop, target)):
            raise SecurityError("live TP/SL prices are not aligned to the exchange tick")
        pending_below_price = (stop * Decimal("0.999") // tick) * tick
        if pending_below_price <= 0 or pending_below_price >= stop:
            raise SecurityError("live stop-limit price is invalid")
        ids = self.client_ids(str(proposal.get("id", "")))
        return {
            "toolName": self.delegated_tool_name,
            "arguments": {
                "symbol": canonical["symbol"], "workingType": "LIMIT", "workingSide": "BUY",
                "workingPrice": entry, "workingQuantity": quantity,
                "workingTimeInForce": "GTC", "workingClientOrderId": ids["working_client_order_id"],
                "pendingSide": "SELL", "pendingQuantity": pending_quantity,
                "pendingAboveType": "TAKE_PROFIT_LIMIT", "pendingAbovePrice": target,
                "pendingAboveStopPrice": target, "pendingAboveTimeInForce": "GTC",
                "pendingAboveClientOrderId": ids["pending_above_client_order_id"],
                "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowStopPrice": stop,
                "pendingBelowPrice": pending_below_price,
                "pendingBelowTimeInForce": "GTC", "pendingBelowClientOrderId": ids["pending_below_client_order_id"],
                "listClientOrderId": ids["order_list_client_id"],
            },
        }

    def execute_partial_exit(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]:
        """One approved, ordered cancel → sell → re-arm workflow; never retries a write."""
        canonical = self.freeze(proposal)
        if canonical.get("source") != "manual-live-partial-exit":
            raise SecurityError("partial exit source is invalid")
        required = ("cancel_order_id", "order_list_id", "sell_quantity", "remaining_quantity", "market_step_size", "price_tick_size", "stop_reference", "take_profit_reference")
        if any(k not in canonical for k in required):
            raise SecurityError("partial exit is missing immutable terms")
        sell = decimal_value(canonical["sell_quantity"], "sell_quantity")
        remaining = decimal_value(canonical["remaining_quantity"], "remaining_quantity")
        step = decimal_value(canonical["market_step_size"], "market_step_size")
        if sell <= 0 or remaining < 0 or step <= 0 or sell % step or remaining % step:
            raise SecurityError("partial exit quantities are invalid")
        ids = self.client_ids(str(proposal.get("id", "")))
        cancel = {"toolName": self.cancel_delegated_tool_name, "arguments": {"symbol": canonical["symbol"], "orderId": canonical["cancel_order_id"], "newClientOrderId": f"sgx-{ids['order_list_client_id'][4:]}"}}
        self.validate_delegated_write_tool(cancel["toolName"])
        cancel_response = self._decode_mcp_result(self._direct_mcp_result(cancel))
        # Binance can acknowledge one OCO leg cancellation with a response shape
        # that lacks the single-order CANCELED status.  The only safe fallback is
        # a fresh read proving that no leg from this exact list remains active.
        try:
            self.validate_write_response(cancel_response, cancel=True)
        except SecurityError:
            remaining_orders = self.read_open_spot_orders()
            if any(row.get("symbol") == canonical["symbol"] and row.get("orderListId") == canonical["order_list_id"] for row in remaining_orders):
                raise SecurityError("partial exit OCO cancellation was not confirmed; reconciliation required")
            cancel_response = {**dict(cancel_response), "status": "CANCELED", "reconciled_by_open_orders": True}
        else:
            if any(row.get("symbol") == canonical["symbol"] and row.get("orderListId") == canonical["order_list_id"] for row in self.read_open_spot_orders()):
                raise SecurityError("partial exit OCO cancellation requires reconciliation")
        base_asset = canonical["symbol"][:-len(self.settings.risk.quote_asset)]
        account = self.read_spot_account()
        free = next((decimal_value(row.get("free", "0"), "free_balance") for row in account["balances"]
                     if row.get("asset") == base_asset), Decimal("0"))
        if (free // step) * step < sell:
            raise SecurityError("free Spot balance changed after OCO cancellation; partial sell was not submitted")
        sell_request = {"toolName": self.close_delegated_tool_name, "arguments": {"symbol": canonical["symbol"], "side": "SELL", "type": "MARKET", "quantity": sell, "newClientOrderId": f"sgc-{ids['order_list_client_id'][4:]}"}}
        self.validate_delegated_write_tool(sell_request["toolName"])
        sell_response = self._decode_mcp_result(self._direct_mcp_result(sell_request))
        self.validate_write_response(sell_response, close=True)
        rearm_response = None
        if remaining > 0:
            tick = decimal_value(canonical["price_tick_size"], "price_tick_size")
            stop = decimal_value(canonical["stop_reference"], "stop_reference")
            target = decimal_value(canonical["take_profit_reference"], "take_profit_reference")
            below = (stop * Decimal("0.999") // tick) * tick
            if tick <= 0 or stop <= 0 or target <= stop or below <= 0:
                raise SecurityError("partial exit re-arm bracket is invalid")
            oco = {"toolName": "spot.orderListOco", "arguments": {"symbol": canonical["symbol"], "side": "SELL", "quantity": remaining, "aboveType": "TAKE_PROFIT_LIMIT", "abovePrice": target, "aboveStopPrice": target, "aboveTimeInForce": "GTC", "aboveClientOrderId": ids["pending_above_client_order_id"], "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": stop, "belowPrice": below, "belowTimeInForce": "GTC", "belowClientOrderId": ids["pending_below_client_order_id"], "listClientOrderId": ids["order_list_client_id"]}}
            self.validate_delegated_write_tool(oco["toolName"])
            rearm_response = self._decode_mcp_result(self._direct_mcp_result(oco))
            self.validate_write_response(rearm_response)
            self.validate_protective_legs(rearm_response, proposal)
        return {"orderId": sell_response["orderId"], "status": sell_response["status"], "partial_exit": {"cancel": cancel_response, "sell": sell_response, "rearm": rearm_response}}

    def readiness(self, *, connected: bool, armed: bool, symbol_flags_verified: bool = False,
                  account_read_verified: bool = False, open_orders_read_verified: bool = False,
                  spot_trade_scope_verified: bool = False, write_tool_discovered: bool = False,
                  write_schema_verified: bool = False) -> LiveReadiness:
        blockers = []
        dedicated_execution_profile = (
            self.settings.codex.mcp_server == self.server
            and self.settings.codex.agent_os_home is not None
            and self.settings.codex.agent_os_workspace is not None
        )
        checks = {
            "binance_mcp_connected": connected and self.settings.codex.mcp_server == self.server,
            "execution_profile_configured": dedicated_execution_profile,
            # Codex's login command reports its own session, not a successful
            # Binance account read. Keep that distinction visible in status.
            "codex_login_reported": connected,
            "account_read_verified": account_read_verified,
            "open_orders_read_verified": open_orders_read_verified,
            # These require independent, server-provided evidence. A local
            # profile or a pinned request dictionary is not such evidence.
            "spot_trade_scope_verified": spot_trade_scope_verified,
            "write_tool_discovered": write_tool_discovered,
            "write_schema_verified": write_schema_verified,
            # The current delegated numeric contract has already produced a
            # Binance -1100 for a valid small decimal.  It is not safe to
            # attest this capability from schema discovery alone.
            "decimal_transport_verified": False,
            "protective_order_capability_verified": (
                self.settings.live.protective_orders_available and symbol_flags_verified
            ),
            "symbol_exchange_flags_verified": symbol_flags_verified,
            # Config loading has already validated relationships and positive
            # values. Readiness must not duplicate one historical USD profile.
            "live_limits_valid": (
                self.settings.live.max_active_tranches > 0
                and self.settings.live.max_economic_positions > 0
                and self.settings.live.max_successful_entries_per_utc_day > 0
                and self.settings.live.max_open_exposure_usdt > 0
                and self.settings.live.max_risk_per_position_usdt > 0
                and self.settings.live.max_aggregate_risk_usdt
                >= self.settings.live.max_risk_per_position_usdt
                and self.settings.live.daily_realized_loss_cap_usdt > 0
            ),
            "live_enabled": self.settings.live.enabled,
            "live_armed": armed,
        }
        for name, value in checks.items():
            if not value: blockers.append(name)
        return LiveReadiness(**checks, execution_ready=all(checks.values()), blockers=tuple(blockers))

    def verify_readiness(self, *, connected: bool, symbol_flags_verified: bool,
                         permission_attestation: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Collect fresh read-only proofs; unavailable backend metadata fails closed."""
        account_ok = open_orders_ok = False
        account_scope_ok = False
        account_type = None
        can_trade = None
        reasons: list[str] = []
        if self.rate_limit_status()["status"] == "BLOCKED":
            return {"account_read_verified": False, "open_orders_read_verified": False,
                    "spot_trade_scope_verified": False, "write_tool_discovered": False,
                    "write_schema_verified": False, "decimal_transport_verified": False,
                    "reasons": ["binance_rate_limit_blocked"],
                    "symbol_flags_verified": symbol_flags_verified,
                    "test_order_schema_fingerprint": None, "account_type": None, "can_trade": None}
        if connected:
            try:
                account = self.read_spot_account(); account_ok = True
                account_type, can_trade = account.get("account_type"), account.get("can_trade")
                account_scope_ok = account_type == "SPOT" and can_trade is True
            except Exception as exc:
                reasons.append(f"account_read:{exc}")
            try:
                self.read_open_spot_orders(); open_orders_ok = True
            except Exception as exc:
                reasons.append(f"open_orders_read:{exc}")
        else:
            reasons.append("backend_not_connected")
        try:
            evidence = self.readiness_probe()
        except Exception as exc:
            evidence = {"reasons": [f"backend_capability_probe:{exc}"]}
        if not isinstance(evidence, Mapping): evidence = {}
        evidence_reasons = evidence.get("reasons", ())
        if isinstance(evidence_reasons, str): evidence_reasons = [evidence_reasons]
        if not isinstance(evidence_reasons, (list, tuple)): evidence_reasons = []
        attestation_ok = self._valid_permission_attestation(
            permission_attestation, evidence.get("test_order_schema_fingerprint"))
        return {"account_read_verified": account_ok, "open_orders_read_verified": open_orders_ok,
                "spot_trade_scope_verified": account_scope_ok and attestation_ok,
                "write_tool_discovered": bool(evidence.get("write_tool_discovered")),
                "write_schema_verified": bool(evidence.get("write_schema_verified")),
                "decimal_transport_verified": False,
                "reasons": reasons + list(evidence_reasons) + ["REMOTE_MCP_DECIMAL_CONTRACT_BLOCKER"],
                "symbol_flags_verified": symbol_flags_verified,
                "test_order_schema_fingerprint": evidence.get("test_order_schema_fingerprint"),
                "account_type": account_type, "can_trade": can_trade}

    def readiness_probe(self) -> Mapping[str, Any]:
        """Verify the generic wrapper, delegated catalog, and fixed schemas."""
        top_level = self._direct_mcp_jsonrpc("tools/list", {})
        tools = top_level.get("tools") if isinstance(top_level, Mapping) else None
        wrapper = next((row for row in tools or [] if isinstance(row, Mapping)
                        and row.get("name") == self.tool_name), None)
        wrapper_error = self._validate_tool_execute_schema(wrapper)
        if wrapper_error:
            return {"reasons": [wrapper_error]}
        by_name = self._delegated_trade_catalog()
        missing = sorted(set(self._REQUIRED_WRITE_SCHEMAS) - set(by_name))
        if missing:
            return {"write_tool_discovered": False, "write_schema_verified": False,
                    "spot_trade_scope_verified": False,
                    "reasons": ["missing_spot_write_tools:" + ",".join(missing)]}
        schema_errors: list[str] = []
        for name, required in self._REQUIRED_WRITE_SCHEMAS.items():
            if not self._schema_matches_fixed_request(name, by_name[name]):
                schema_errors.append(name)
        if schema_errors:
            return {"write_tool_discovered": True, "write_schema_verified": False,
                    "spot_trade_scope_verified": False,
                    "reasons": ["incompatible_spot_write_schemas:" + ",".join(sorted(schema_errors))]}
        permission = self._discover_api_permission_proof(by_name)
        return {"write_tool_discovered": True, "write_schema_verified": True,
                "spot_trade_scope_verified": bool(permission.get("verified")),
                "permission_metadata": permission,
                "reasons": list(permission.get("reasons", ())),
                "delegated_tools": sorted(by_name),
                "test_order_schema_fingerprint": permission.get("test_order_schema_fingerprint")}

    @classmethod
    def _validate_tool_execute_schema(cls, row: Mapping[str, Any] | None) -> str | None:
        if row is None:
            return "tool_execute_missing_from_mcp_tools_list"
        schema = row.get("inputSchema")
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            return "tool_execute_schema_not_object"
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, Mapping) or not isinstance(required, list):
            return "tool_execute_schema_is_incomplete"
        if "toolName" not in properties or properties["toolName"].get("type") != "string":
            return "tool_execute_toolName_schema_invalid"
        if "arguments" not in properties or properties["arguments"].get("type") != "object":
            return "tool_execute_arguments_schema_invalid"
        if "toolName" not in required or schema.get("additionalProperties") is not False:
            return "tool_execute_schema_is_not_strict"
        return None

    def _delegated_trade_catalog(self) -> dict[str, Mapping[str, Any]]:
        now = time.monotonic()
        if self._delegated_catalog_cache and now - self._delegated_catalog_cache[0] <= self._CATALOG_CACHE_TTL_SECONDS:
            return dict(self._delegated_catalog_cache[1])
        catalog = self._search_catalog("trade")
        self._delegated_catalog_cache = (now, dict(catalog))
        return catalog

    def _search_catalog(self, category: str) -> dict[str, Mapping[str, Any]]:
        catalog: dict[str, Mapping[str, Any]] = {}
        cursor: str | None = None
        item_count = 0
        for _ in range(self._TOOL_SEARCH_MAX_PAGES):
            arguments: dict[str, Any] = {"category": category}
            if cursor: arguments["cursor"] = cursor
            result = self._decode_mcp_result(self._direct_mcp_jsonrpc(
                "tools/call", {"name": "tool_search", "arguments": arguments}))
            if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
                raise SecurityError("delegated trade catalog response is malformed")
            for row in result["tools"]:
                item_count += 1
                if item_count > self._TOOL_SEARCH_MAX_ITEMS:
                    raise SecurityError("delegated trade catalog exceeded safety limit")
                if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                    catalog[row["name"]] = row
            cursor = result.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        else:
            raise SecurityError("delegated trade catalog exceeded page safety limit")
        return catalog

    @classmethod
    def _schema_matches_fixed_request(cls, name: str, row: Mapping[str, Any]) -> bool:
        schema = row.get("inputSchema")
        if not isinstance(schema, Mapping) or schema.get("type") != "object": return False
        properties = schema.get("properties")
        if not isinstance(properties, Mapping): return False
        expected = cls._REQUIRED_WRITE_SCHEMAS[name]
        if not expected <= set(properties): return False
        required = schema.get("required", [])
        if not isinstance(required, list) or not set(required) <= expected: return False
        if schema.get("additionalProperties") is True: return False
        expected_types = {"symbol":"string", "side":"string", "type":"string", "quantity":"number",
                          "price":"number", "stopPrice":"number", "timeInForce":"string",
                          "newClientOrderId":"string", "orderId":"integer"}
        for field, field_type in expected_types.items():
            if field in expected and properties[field].get("type") != field_type: return False
        expected_values = {"side":{"SELL"}, "type":{"MARKET"}, "aboveType":{"TAKE_PROFIT_LIMIT"},
                           "belowType":{"STOP_LOSS_LIMIT"}, "pendingSide":{"SELL"},
                           "pendingAboveType":{"TAKE_PROFIT_LIMIT"}, "pendingBelowType":{"STOP_LOSS_LIMIT"},
                           "workingSide":{"BUY"}, "workingType":{"LIMIT"}}
        for field, values in expected_values.items():
            if field not in expected: continue
            enum = properties[field].get("enum")
            if isinstance(enum, list) and not values <= set(enum): return False
        return True

    def _discover_api_permission_proof(self, trade_catalog: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        """Find and read an account permission operation only by its schema."""
        try:
            rows = list(self._search_catalog("account").values())
        except Exception as exc:
            return {"verified": False, "reasons": [f"api_restrictions_discovery:{exc}"]}
        candidate = next((row for row in rows if isinstance(row, Mapping)
                          and isinstance(row.get("inputSchema"), Mapping)
                          and self._API_PERMISSION_FIELDS <= set(row["inputSchema"].get("properties", {}))), None)
        if candidate is None:
            test_order = trade_catalog.get("spot.orderTest")
            test_order_available = isinstance(test_order, Mapping) and self._schema_matches_test_order(test_order)
            return {"verified": False, "test_order_equivalent": test_order_available,
                    "test_order_schema_fingerprint": self._schema_fingerprint(test_order) if test_order_available else None,
                    "reasons": ["api_restrictions_equivalent_missing"] +
                    (["test_order_permission_attestation_available_but_not_run"] if test_order_available else [])}
        name = candidate.get("name")
        try:
            response = self._decode_mcp_result(self._direct_mcp_result({"toolName": name, "arguments": {}}))
        except Exception as exc:
            return {"verified": False, "tool": name, "reasons": [f"api_restrictions_read:{exc}"]}
        if not isinstance(response, Mapping) or any(not isinstance(response.get(field), bool) for field in self._API_PERMISSION_FIELDS):
            return {"verified": False, "tool": name, "reasons": ["api_restrictions_response_invalid"]}
        verified = response["enableReading"] and response["enableSpotAndMarginTrading"]
        return {"verified": verified, "tool": name,
                "flags": {field: response[field] for field in sorted(self._API_PERMISSION_FIELDS)},
                "reasons": [] if verified else ["api_restrictions_spot_trade_not_enabled"]}

    @classmethod
    def validate_delegated_write_tool(cls, tool_name: str) -> None:
        if tool_name not in cls._ALLOWED_WRITE_TOOLS:
            raise SecurityError("delegated write target is not an approved Spot operation")

    @staticmethod
    def _schema_matches_test_order(row: Mapping[str, Any]) -> bool:
        schema = row.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(schema, Mapping) or schema.get("type") != "object" or not isinstance(props, Mapping):
            return False
        expected = {"symbol": "string", "side": "string", "type": "string",
                    "quantity": "number", "quoteOrderQty": "number", "price": "number", "timeInForce": "string"}
        if not set(expected) <= set(props): return False
        if any(props[name].get("type") != field_type for name, field_type in expected.items()):
            return False
        required = schema.get("required", [])
        return isinstance(required, list) and set(required) <= set(expected) and (
            "quantity" in props or "quoteOrderQty" in props)

    @staticmethod
    def _schema_fingerprint(row: Mapping[str, Any] | None) -> str | None:
        schema = row.get("inputSchema") if isinstance(row, Mapping) else None
        if not isinstance(schema, Mapping):
            return None
        return __import__("hashlib").sha256(canonical_json(dict(schema)).encode()).hexdigest()

    def _valid_permission_attestation(self, proof: Mapping[str, Any] | None,
                                      schema_fingerprint: str | None) -> bool:
        if not isinstance(proof, Mapping) or proof.get("result") != "verified":
            return False
        if proof.get("delegated_operation") != self.permission_test_tool_name:
            return False
        if not schema_fingerprint or proof.get("schema_fingerprint") != schema_fingerprint:
            return False
        if proof.get("profile_fingerprint") != self.execution_profile_fingerprint():
            return False
        try:
            return parse_time(proof.get("expires_at")) > utcnow()
        except (TypeError, ValueError):
            return False

    def execution_profile_fingerprint(self) -> str:
        identity = {"server": self.settings.codex.mcp_server,
                    "endpoint": "https://agent.binance.com/mcp/agentic",
                    "home": str(self.settings.codex.agent_os_home),
                    "workspace": str(self.settings.codex.agent_os_workspace)}
        return __import__("hashlib").sha256(canonical_json(identity).encode()).hexdigest()

    def attest_spot_trade_permission(self, symbol: str, exchange: Mapping[str, Any],
                                     market: Any) -> dict[str, Any]:
        """Perform exactly one explicit, non-matching-engine Spot order test."""
        if symbol not in self.settings.live.allowed_symbols:
            raise SecurityError("permission attestation symbol is not allowlisted")
        top_level = self._direct_mcp_jsonrpc("tools/list", {})
        tools = top_level.get("tools") if isinstance(top_level, Mapping) else []
        wrapper = next((row for row in tools if isinstance(row, Mapping) and row.get("name") == self.tool_name), None)
        wrapper_error = self._validate_tool_execute_schema(wrapper)
        catalog = self._delegated_trade_catalog()
        row = catalog.get(self.permission_test_tool_name)
        if wrapper_error or not isinstance(row, Mapping) or not self._schema_matches_test_order(row):
            raise SecurityError("spot.orderTest discovery/schema proof is unavailable")
        tick = decimal_value(exchange.get("price_tick_size"), "price_tick_size")
        step = decimal_value(exchange.get("market_step_size"), "market_step_size")
        minimum = decimal_value(exchange.get("min_notional"), "min_notional")
        price = (Decimal(str(market.ask)) / tick).to_integral_value(rounding="ROUND_CEILING") * tick
        if min(price, step, minimum) <= 0:
            raise SecurityError("permission attestation payload filters are invalid")
        quote_order_qty = minimum.to_integral_value(rounding="ROUND_CEILING")
        if quote_order_qty < minimum:
            quote_order_qty += 1
        payload = {"symbol": symbol, "side": "BUY", "type": "MARKET", "quoteOrderQty": quote_order_qty}
        payload_error = self._validate_test_order_arguments(payload, row)
        if payload_error:
            return {"classification": "PAYLOAD_VALIDATION_FAILURE", "stage": "BEFORE_TOOL_EXECUTE",
                    "detail": payload_error, "delegated_tool": self.permission_test_tool_name,
                    "symbol": symbol, "payload": payload}
        try:
            response = self._direct_mcp_jsonrpc("tools/call", {"name": self.tool_name,
                "arguments": {"toolName": self.permission_test_tool_name, "arguments": payload}})
        except MCPTransportError as exc:
            return {"classification": "TRANSPORT_FAILURE", "stage": exc.stage,
                    "error_code": exc.error_code, "http_status": exc.http_status,
                    "detail": self._sanitize_error_text(exc), "delegated_tool": self.permission_test_tool_name,
                    "symbol": symbol, "payload": payload}
        except Exception as exc:
            return {"classification": "TRANSPORT_FAILURE", "stage": "UNKNOWN", "detail": str(exc)[:240],
                    "delegated_tool": self.permission_test_tool_name, "symbol": symbol, "payload": payload}
        detail = self._mcp_error_text(response)
        if response.get("isError") is True or detail:
            lowered = detail.lower()
            auth = any(term in lowered for term in ("unauthor", "forbidden", "permission", "-2014", "-2015", "trade permission"))
            classification = "AUTHORIZATION_FAILURE" if auth else "PAYLOAD_VALIDATION_FAILURE" if any(term in lowered for term in ("filter", "invalid", "quantity", "price", "notional", "-1013", "-1100")) else "TRANSPORT_FAILURE"
            return {"classification": classification, "stage": "REMOTE_DELEGATED_TOOL", "detail": detail[:240], "delegated_tool": self.permission_test_tool_name,
                    "symbol": symbol, "payload": payload}
        return {"classification": "SUCCESS", "stage": "REMOTE_DELEGATED_TOOL", "delegated_tool": self.permission_test_tool_name,
                "symbol": symbol, "payload": payload, "schema_fingerprint": self._schema_fingerprint(row)}

    @staticmethod
    def _validate_test_order_arguments(payload: Mapping[str, Any], row: Mapping[str, Any]) -> str | None:
        schema = row.get("inputSchema")
        props = schema.get("properties") if isinstance(schema, Mapping) else None
        required = schema.get("required", []) if isinstance(schema, Mapping) else None
        if not isinstance(props, Mapping) or not isinstance(required, list):
            return "spot.orderTest schema is incomplete"
        if any(field not in payload for field in required):
            return "spot.orderTest payload omits a schema-required field"
        if schema.get("additionalProperties") is False and any(field not in props for field in payload):
            return "spot.orderTest payload contains an unsupported field"
        for field, value in payload.items():
            expected = props.get(field, {}).get("type") if isinstance(props.get(field), Mapping) else None
            if expected == "string" and not isinstance(value, str): return f"spot.orderTest field {field} is not a string"
            if expected == "number" and (isinstance(value, bool) or not isinstance(value, (int, float, Decimal))): return f"spot.orderTest field {field} is not numeric"
            enum = props.get(field, {}).get("enum") if isinstance(props.get(field), Mapping) else None
            if isinstance(enum, list) and value not in enum: return f"spot.orderTest field {field} is outside its schema enum"
        return None

    @staticmethod
    def _mcp_error_text(response: Mapping[str, Any]) -> str:
        """Return diagnostics only for MCP error semantics, not result content.

        A successful delegated tool may legitimately return a text content block
        such as ``{}''.  Content is therefore evidence of an error only when the
        MCP result marks itself as an error, or when the text has an explicit
        error signature (for backends that omit ``isError``).
        """
        parts = []
        error = response.get("error")
        if isinstance(error, Mapping):
            if error.get("code") is not None:
                parts.append(f"code={error['code']}")
            if error.get("message"):
                parts.append(LiveExecutionAdapter._sanitize_error_text(error["message"]))

        include_content = response.get("isError") is True
        content = response.get("content")
        if isinstance(content, list):
            for row in content:
                if not isinstance(row, Mapping) or not isinstance(row.get("text"), str):
                    continue
                text = row["text"].strip()
                if include_content or LiveExecutionAdapter._looks_like_mcp_error(text):
                    parts.append(LiveExecutionAdapter._sanitize_error_text(text))

        for key in ("structuredContent", "structured_content"):
            structured = response.get(key)
            if isinstance(structured, Mapping) and structured.get("code") not in (None, 0, 200):
                parts.append(LiveExecutionAdapter._sanitize_error_text(
                    structured.get("msg") or structured.get("message") or structured.get("code")))
        return " ".join(parts)

    @staticmethod
    def _looks_like_mcp_error(text: str) -> bool:
        lowered = text.lower()
        return bool(re.search(r"(?:^|\s)-(?:1003|1013|1100|2014|2015)(?:\s|$)", lowered)) or any(
            term in lowered for term in ("permission denied", "unauthorized", "forbidden", "invalid quantity",
                                          "invalid price", "rate limit", "too many requests", "ip banned"))

    @staticmethod
    def _sanitize_error_text(value: Any) -> str:
        text = str(value)[:200]
        return re.sub(r"(?i)(bearer\s+|token|secret|cookie|signature|api[_-]?key)\s*[:=]?\s*[^\s,;]+", r"\1REDACTED", text)

    @classmethod
    def _canonical_wire_json(cls, value: Any, *, field: str | None = None) -> str:
        """Encode Binance decimal parameters as fixed-point JSON numbers."""
        if field in cls._DECIMAL_FIELDS and isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
            return format(value if isinstance(value, Decimal) else Decimal(str(value)), "f")
        if isinstance(value, Mapping):
            return "{" + ",".join(json.dumps(str(key), ensure_ascii=False) + ":" + cls._canonical_wire_json(item, field=str(key))
                                for key, item in value.items()) + "}"
        if isinstance(value, list):
            return "[" + ",".join(cls._canonical_wire_json(item) for item in value) + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    @staticmethod
    def validate_active_protective_oco(orders: list[Mapping[str, Any]], symbol: str,
                                       *, tick: Decimal | None = None) -> dict[str, Any]:
        """Validate one exact active two-leg Spot SELL protective OCO."""
        if len(orders) != 2:
            raise SecurityError("active protection is not exactly two OCO legs")
        list_ids = {row.get("orderListId") for row in orders}
        list_id = next(iter(list_ids), None)
        if len(list_ids) != 1 or not isinstance(list_id, int) or list_id <= 0:
            raise SecurityError("active protection has an invalid orderListId")
        if any(row.get("symbol") != symbol or row.get("side") != "SELL" for row in orders):
            raise SecurityError("active protection is not a same-symbol SELL OCO")
        if any(row.get("status") not in {"NEW", "PENDING_NEW"} for row in orders):
            raise SecurityError("active protection contains an inactive OCO leg")
        if {row.get("type") for row in orders} != {"STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"}:
            raise SecurityError("active protection has unexpected OCO order types")
        if any(not isinstance(row.get("orderId"), int) or row.get("orderId") <= 0 for row in orders):
            raise SecurityError("active protection has an invalid orderId")
        quantities = {decimal_value(row.get("origQty"), "protective_quantity") for row in orders}
        if len(quantities) != 1 or next(iter(quantities)) <= 0:
            raise SecurityError("active OCO legs do not protect one positive quantity")
        stop_leg = next(row for row in orders if row.get("type") == "STOP_LOSS_LIMIT")
        target_leg = next(row for row in orders if row.get("type") == "TAKE_PROFIT_LIMIT")
        stop = decimal_value(stop_leg.get("stopPrice"), "stop_price")
        target = decimal_value(target_leg.get("stopPrice") or target_leg.get("price"), "target_price")
        if stop <= 0 or target <= stop or (tick is not None and (stop % tick or target % tick)):
            raise SecurityError("active OCO stop/target prices are invalid")
        client_ids = [row.get("clientOrderId") for row in orders if row.get("clientOrderId") is not None]
        if client_ids and any(not isinstance(value, str) or not value.startswith(("sgt-", "sgs-")) for value in client_ids):
            raise SecurityError("active OCO client-order provenance is invalid")
        return {"order_list_id": list_id, "quantity": next(iter(quantities)),
                "stop": stop, "target": target, "stop_leg": stop_leg, "target_leg": target_leg}

    @staticmethod
    def client_ids(proposal_id: str) -> dict[str, str]:
        if not proposal_id.startswith("p-") or len(proposal_id) != 14:
            raise SecurityError("proposal ID is invalid for deterministic client IDs")
        suffix = proposal_id[2:]
        return {"working_client_order_id": f"sgw-{suffix}",
            "pending_above_client_order_id": f"sgt-{suffix}",
            "pending_below_client_order_id": f"sgs-{suffix}",
            "order_list_client_id": f"sgl-{suffix}"}

    @staticmethod
    def validate_write_response(response: Mapping[str, Any], *, close: bool = False, cancel: bool = False) -> str:
        if cancel:
            if not isinstance(response, Mapping) or not isinstance(response.get("orderId"), int) or response.get("status") != "CANCELED":
                raise SecurityError("live OCO cancellation was not confirmed; reconciliation required")
            return "CANCELED"
        if close:
            if not isinstance(response, Mapping) or not isinstance(response.get("orderId"), int):
                raise SecurityError("malformed live Spot close response")
            status = response.get("status")
            # A market-close acknowledgement is not proof that its requested
            # quantity filled.  Re-arming from a precomputed residual after a
            # partial fill can leave the account unprotected or over-reserved.
            if status != "FILLED":
                raise SecurityError("live Spot close is not FILLED; reconciliation required; do not retry")
            return "FILLED"
        if not isinstance(response, Mapping) or not isinstance(response.get("orderListId"), int):
            raise SecurityError("malformed protected Spot write response")
        status = response.get("listStatusType")
        if status not in {"EXEC_STARTED", "ALL_DONE", "RESPONSE"}:
            raise SecurityError("ambiguous protected Spot response requires reconciliation; do not retry")
        return str(status)

    def validate_protective_legs(self, response: Mapping[str, Any], proposal: Mapping[str, Any]) -> None:
        """Require pending TP/SL legs to be active, or a leg to have already filled."""
        reports = response.get("orderReports")
        ids = self.client_ids(str(proposal.get("id", "")))
        if not isinstance(reports, list):
            raise SecurityError("protected Spot response has no leg reports; reconciliation required")
        pending = {str(row.get("clientOrderId")): str(row.get("status"))
                   for row in reports if isinstance(row, Mapping)}
        target_status = pending.get(ids["pending_above_client_order_id"])
        stop_status = pending.get(ids["pending_below_client_order_id"])
        if target_status is None or stop_status is None:
            raise SecurityError("protected Spot response omitted TP/SL legs; reconciliation required")
        if "REJECTED" in {target_status, stop_status}:
            raise SecurityError("critical: entry may be filled but Binance rejected a TP/SL leg; reconciliation required")
        active = {"NEW", "PENDING_NEW"}
        if target_status not in active | {"FILLED"} or stop_status not in active | {"FILLED", "CANCELED"}:
            raise SecurityError("protected Spot TP/SL leg status is unsafe; reconciliation required")

    def reconcile(self, proposal_id: str) -> None:
        """Refuse generic automatic reconciliation rather than guessing exchange state."""
        self.client_ids(proposal_id)
        raise SecurityError(
            "generic automatic reconciliation is unavailable; outcome remains unknown and must not be retried"
        )

    def _direct_mcp_jsonrpc(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call one JSON-RPC MCP method directly; readiness uses this read-only."""
        status = self.rate_limit_status()
        if status["status"] == "BLOCKED":
            raise RateLimitBlockedError("Binance MCP rate-limit circuit is blocked")
        home = self.settings.codex.agent_os_home
        if home is None:
            raise SecurityError("dedicated Binance execution profile is not configured")
        try:
            credentials = json.loads((Path(home) / ".credentials.json").read_text(encoding="utf-8"))
            record = next(value for key, value in credentials.items()
                          if key.startswith(f"{self.server}|") and isinstance(value, Mapping)
                          and isinstance(value.get("access_token"), str))
            token = record["access_token"]
        except (OSError, ValueError, StopIteration, TypeError) as exc:
            raise SecurityError("dedicated Binance OAuth credential is unavailable") from exc
        envelope = {"jsonrpc": "2.0", "id": 1, "method": method, "params": dict(params)}
        http_request = urllib.request.Request(
            "https://agent.binance.com/mcp/agentic", data=self._canonical_wire_json(envelope).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(http_request, timeout=self.settings.codex.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code in {418, 429}:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                until = None
                try:
                    until = isoformat(utcnow() + timedelta(seconds=int(retry_after))) if retry_after else None
                except (TypeError, ValueError):
                    until = None
                self._activate_rate_limit(reason=f"HTTP_{exc.code}", blocked_until=until)
            raise MCPTransportError("protected Binance MCP HTTP transport failed; reconciliation required",
                                    stage="MCP_HTTP", http_status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise MCPTransportError("protected Binance MCP transport did not complete; reconciliation required",
                                    stage="MCP_TRANSPORT") from exc
        if not isinstance(payload, Mapping) or payload.get("error"):
            error = payload.get("error") if isinstance(payload, Mapping) else None
            if isinstance(error, Mapping):
                code = error.get("code")
                message = error.get("message")
                detail = f"code={code}" + (f" message={self._sanitize_error_text(message)}" if message else "")
            else:
                detail = "invalid_jsonrpc_error"
            if self._is_rate_limit_error(code if isinstance(error, Mapping) else None, detail):
                self._activate_rate_limit(reason="BINANCE_-1003" if "-1003" in detail else "BINANCE_RATE_LIMIT",
                                          blocked_until=self._blocked_until_from_detail(detail))
            raise MCPTransportError(f"protected Binance MCP returned an error ({detail}); reconciliation required",
                                    stage="REMOTE_JSONRPC", error_code=code if isinstance(error, Mapping) else None)
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise SecurityError("Binance MCP returned an invalid JSON-RPC result")
        return result

    def _direct_mcp_result(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._direct_mcp_jsonrpc("tools/call", {"name": self.tool_name, "arguments": dict(request)})
        if result.get("isError") is True:
            detail = self._mcp_error_text(result)
            if self._is_rate_limit_error(None, detail):
                self._activate_rate_limit(reason="BINANCE_-1003" if "-1003" in detail else "BINANCE_RATE_LIMIT")
            raise SecurityError("protected Binance MCP returned an invalid result; reconciliation required")
        return result

    @classmethod
    def _is_rate_limit_error(cls, code: Any, detail: Any) -> bool:
        text = str(detail).lower()
        return code == -1003 or any(term in text for term in (
            "-1003", "http 429", "http 418", "request weight", "rate limit", "too many requests", "ip banned", "temporarily banned"))

    @staticmethod
    def _blocked_until_from_detail(detail: str) -> str | None:
        match = re.search(r"(?:banned|ban)\s+until\s+(\d{10,})", detail, re.IGNORECASE)
        if not match:
            return None
        try:
            return isoformat(datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc))
        except (ValueError, OverflowError, OSError):
            return None

    @property
    def _rate_limit_state_path(self) -> Path:
        return Path(self.settings.state_dir) / self._RATE_LIMIT_STATE_FILE

    def _activate_rate_limit(self, *, reason: str, blocked_until: str | None = None) -> None:
        state = {"status": "BLOCKED", "reason": reason,
                 "blocked_until": blocked_until if isinstance(blocked_until, str) else None}
        path = self._rate_limit_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        temporary.replace(path)

    def rate_limit_status(self) -> dict[str, Any]:
        try:
            state = json.loads(self._rate_limit_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"status": "CLEAR", "reason": None, "blocked_until": None}
        if not isinstance(state, Mapping) or state.get("status") != "BLOCKED":
            return {"status": "CLEAR", "reason": None, "blocked_until": None}
        blocked_until = state.get("blocked_until")
        if isinstance(blocked_until, str):
            try:
                if parse_time(blocked_until) <= utcnow():
                    return {"status": "CLEAR", "reason": None, "blocked_until": blocked_until}
            except (TypeError, ValueError):
                blocked_until = None
        return {"status": "BLOCKED", "reason": state.get("reason", "BINANCE_RATE_LIMIT"),
                "blocked_until": blocked_until if isinstance(blocked_until, str) else "UNKNOWN"}

    def clear_rate_limit_circuit(self) -> dict[str, Any]:
        """Explicit operator action; it never performs a Binance request."""
        try:
            self._rate_limit_state_path.unlink()
        except FileNotFoundError:
            pass
        return self.rate_limit_status()

    @staticmethod
    def _decode_mcp_result(result: Mapping[str, Any]) -> Any:
        structured = result.get("structured_content", result.get("structuredContent"))
        if isinstance(structured, Mapping):
            return structured
        content = result.get("content")
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], Mapping)
                and isinstance(content[0].get("text"), str)):
            try:
                decoded = json.loads(content[0]["text"])
            except json.JSONDecodeError as exc:
                raise SecurityError("protected Binance MCP response is malformed; reconciliation required") from exc
            if isinstance(decoded, (Mapping, list)):
                return decoded
        raise SecurityError("protected Binance MCP response is malformed; reconciliation required")

    def execute(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]:
        """Submit one approved protected request through the dedicated Binance MCP transport.

        The caller supplies no endpoint or arguments; both are derived from the
        immutable proposal. A failed or ambiguous transport result is surfaced
        to the service so it can mark the proposal RECONCILE instead of retrying
        a request that Binance may already have accepted.
        """
        request = self.protected_request(proposal)
        self.validate_delegated_write_tool(request["toolName"])
        home = self.settings.codex.agent_os_home
        workspace = self.settings.codex.agent_os_workspace
        if home is None or workspace is None:
            raise SecurityError("dedicated Binance execution profile is not configured")
        server = self.settings.codex.mcp_server
        self.validate_tool(server, self.tool_name)
        response = self._decode_mcp_result(self._direct_mcp_result(request))
        source = proposal.get("canonical", {}).get("source")
        is_close = source == "manual-live-close"
        is_cancel = source == "manual-live-cancel-protection"
        self.validate_write_response(response, close=is_close, cancel=is_cancel)
        if not is_close and not is_cancel:
            self.validate_protective_legs(response, proposal)
        return dict(response)
    def read_spot_account(self) -> dict[str, Any]:
        response = self._read_exact("account")
        balances = response.get("balances") if isinstance(response, Mapping) else None
        if not isinstance(balances, list):
            raise SecurityError("Spot account response is malformed")
        rows = []
        for row in balances:
            if not isinstance(row, Mapping) or not isinstance(row.get("asset"), str):
                raise SecurityError("Spot account balance response is malformed")
            free, locked = decimal_value(row.get("free"), "free"), decimal_value(row.get("locked"), "locked")
            if free < 0 or locked < 0:
                raise SecurityError("Spot account balance is invalid")
            if free or locked:
                rows.append({"asset": row["asset"], "free": format(free, "f"), "locked": format(locked, "f")})
        account_type = response.get("accountType")
        permissions = response.get("permissions")
        scope_verified = (account_type == "SPOT" and response.get("canTrade") is True
                          and isinstance(permissions, list) and permissions == ["SPOT"])
        return {"account_type": account_type if isinstance(account_type, str) else None,
                "can_trade": response.get("canTrade") if isinstance(response.get("canTrade"), bool) else None,
                "balances": rows,
                "spot_trade_scope_verified": scope_verified}

    def read_open_spot_orders(self) -> list[dict[str, Any]]:
        response = self._read_exact("open_orders")
        if not isinstance(response, list):
            raise SecurityError("open Spot orders response is malformed")
        fields = ("symbol", "orderId", "orderListId", "clientOrderId", "side", "type", "timeInForce",
                  "price", "stopPrice", "origQty", "executedQty", "status")
        if any(not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str) for row in response):
            raise SecurityError("open Spot order response is malformed")
        return [{field: row[field] for field in fields if field in row} for row in response]

    def read_spot_trades(self, symbol: str, start_time_ms: int) -> list[dict[str, Any]]:
        """Read fixed Spot trade evidence; callers never control the tool name."""
        if symbol not in self.settings.live.allowed_symbols or start_time_ms <= 0:
            raise SecurityError("unapproved live trade-history read")
        request = {"toolName": "spot.getMyTrades", "arguments": {
            "symbol": symbol, "startTime": start_time_ms, "limit": 1000,
        }}
        response = self._decode_mcp_result(self._direct_mcp_result(request))
        if not isinstance(response, list):
            raise SecurityError("Spot trade history response is malformed")
        if len(response) >= 1000:
            raise SecurityError("Spot trade history is truncated; reconciliation required")
        trades: list[dict[str, Any]] = []
        for row in response:
            if not isinstance(row, Mapping) or not isinstance(row.get("isBuyer"), bool):
                raise SecurityError("Spot trade history row is malformed")
            quantity = decimal_value(row.get("qty"), "trade_qty")
            quote = decimal_value(row.get("quoteQty"), "trade_quote_qty")
            if quantity <= 0 or quote <= 0:
                raise SecurityError("Spot trade history row is invalid")
            trades.append({"isBuyer": row["isBuyer"], "qty": format(quantity, "f"),
                           "quoteQty": format(quote, "f"), "time": row.get("time")})
        return trades

    def _read_exact(self, kind: str) -> Any:
        if kind not in self._READ_REQUESTS:
            raise SecurityError("unapproved live account read")
        tool_name, arguments = self._READ_REQUESTS[kind]
        home, workspace = self.settings.codex.agent_os_home, self.settings.codex.agent_os_workspace
        if home is None or workspace is None:
            raise SecurityError("dedicated Binance execution profile is not configured")
        request = {"toolName": tool_name, "arguments": arguments}
        return self._decode_mcp_result(self._direct_mcp_result(request))

    @classmethod
    def validate_tool(cls, server: str, tool: str) -> None:
        text = f"{server}:{tool}".lower()
        if server != cls.server or tool != cls.tool_name or any(x in text for x in FORBIDDEN_FAMILIES):
            raise SecurityError("MCP write target is not the exact approved protected Binance Spot tool")
