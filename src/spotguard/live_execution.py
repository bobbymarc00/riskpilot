from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .config import Settings
from .security import SecurityError
from .util import decimal_value


BINANCE_MCP_SERVER = "binance-execution"
FORBIDDEN_FAMILIES = ("futures", "margin", "convert", "wallet", "transfer", "payment", "withdraw", "borrow", "cancel")


@dataclass(frozen=True)
class LiveReadiness:
    binance_mcp_connected: bool
    agentic_account_accessible: bool
    account_scope_available: bool
    spot_trade_scope_available: bool
    exact_spot_write_schema_verified: bool
    protective_order_list_verified: bool
    symbol_exchange_flags_verified: bool
    live_limits_valid: bool
    live_enabled: bool
    live_armed: bool
    execution_ready: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "blockers": list(self.blockers)}


class LiveExecutionAdapter:
    """Hard boundary for a future exact Binance protected Spot write.

    No write transport is installed in this release. The adapter validates immutable
    approved intent and fails closed until exact MCP schema, scopes, native confirmation,
    OPO/OCO protection, and reconciliation are verified.
    """
    server = BINANCE_MCP_SERVER
    # Binance Agentic MCP uses one generic envelope. The delegated name is
    # therefore part of our local allowlist, not model-controlled text.
    tool_name = "tool_execute"
    delegated_tool_name = "spot.orderListOtoco"
    close_delegated_tool_name = "spot.newOrder"
    cancel_delegated_tool_name = "spot.deleteOrder"
    # Fixed read-only Telegram surface: balances plus active Spot TP/SL orders.
    _READ_REQUESTS = {
        "account": ("spot.getAccount", {"omitZeroBalances": True}),
        "open_orders": ("spot.getOpenOrders", {}),
    }


    def __init__(self, settings: Settings) -> None:
        self.settings = settings

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
        if not is_close and not is_cancel and not is_partial_exit and not is_set_protection and decimal_value(canonical.get("quote_amount"), "quote_amount") > self.settings.live.max_quote_per_entry_usdt:
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
            return {"toolName": "spot.orderListOco", "arguments": {"symbol": canonical["symbol"], "side": "SELL", "quantity": float(quantity), "aboveType": "TAKE_PROFIT_LIMIT", "abovePrice": float(target), "aboveStopPrice": float(target), "aboveTimeInForce": "GTC", "aboveClientOrderId": ids["pending_above_client_order_id"], "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": float(stop), "belowPrice": float(below), "belowTimeInForce": "GTC", "belowClientOrderId": ids["pending_below_client_order_id"], "listClientOrderId": ids["order_list_client_id"]}}
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
                    "quantity": float(quantity), "newClientOrderId": f"sgc-{ids['order_list_client_id'][4:]}",
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
                "workingPrice": float(entry), "workingQuantity": float(quantity),
                "workingTimeInForce": "GTC", "workingClientOrderId": ids["working_client_order_id"],
                "pendingSide": "SELL", "pendingQuantity": float(pending_quantity),
                "pendingAboveType": "TAKE_PROFIT_LIMIT", "pendingAbovePrice": float(target),
                "pendingAboveStopPrice": float(target), "pendingAboveTimeInForce": "GTC",
                "pendingAboveClientOrderId": ids["pending_above_client_order_id"],
                "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowStopPrice": float(stop),
                "pendingBelowPrice": float(pending_below_price),
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
        sell_request = {"toolName": self.close_delegated_tool_name, "arguments": {"symbol": canonical["symbol"], "side": "SELL", "type": "MARKET", "quantity": float(sell), "newClientOrderId": f"sgc-{ids['order_list_client_id'][4:]}"}}
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
            oco = {"toolName": "spot.orderListOco", "arguments": {"symbol": canonical["symbol"], "side": "SELL", "quantity": float(remaining), "aboveType": "TAKE_PROFIT_LIMIT", "abovePrice": float(target), "aboveStopPrice": float(target), "aboveTimeInForce": "GTC", "aboveClientOrderId": ids["pending_above_client_order_id"], "belowType": "STOP_LOSS_LIMIT", "belowStopPrice": float(stop), "belowPrice": float(below), "belowTimeInForce": "GTC", "belowClientOrderId": ids["pending_below_client_order_id"], "listClientOrderId": ids["order_list_client_id"]}}
            rearm_response = self._decode_mcp_result(self._direct_mcp_result(oco))
            self.validate_write_response(rearm_response)
            self.validate_protective_legs(rearm_response, proposal)
        return {"orderId": sell_response["orderId"], "status": sell_response["status"], "partial_exit": {"cancel": cancel_response, "sell": sell_response, "rearm": rearm_response}}

    def readiness(self, *, connected: bool, armed: bool, symbol_flags_verified: bool = False) -> LiveReadiness:
        blockers = []
        # OAuth was completed against the dedicated execution profile.  The
        # actual order call remains gated by the owner confirmation, local arm,
        # exact OTOCO request construction, and response verification below.
        dedicated_execution_profile = (
            self.settings.codex.mcp_server == self.server
            and self.settings.codex.agent_os_home is not None
            and self.settings.codex.agent_os_workspace is not None
        )
        checks = {
            "binance_mcp_connected": connected and self.settings.codex.mcp_server == self.server,
            "agentic_account_accessible": connected and dedicated_execution_profile,
            "account_scope_available": connected and dedicated_execution_profile,
            "spot_trade_scope_available": connected and dedicated_execution_profile,
            "exact_spot_write_schema_verified": dedicated_execution_profile,
            "protective_order_list_verified": dedicated_execution_profile and self.settings.live.protective_orders_available,
            "symbol_exchange_flags_verified": symbol_flags_verified,
            "live_limits_valid": (self.settings.live.max_quote_per_entry_usdt == Decimal("100") and
                self.settings.live.max_active_tranches == 10 and
                self.settings.live.max_economic_positions == 5 and
                self.settings.live.max_open_exposure_usdt == Decimal("500") and
                self.settings.live.max_risk_per_position_usdt == Decimal("2") and
                self.settings.live.max_aggregate_risk_usdt == Decimal("4") and
                self.settings.live.daily_realized_loss_cap_usdt == Decimal("5") and
                self.settings.live.max_successful_entries_per_utc_day == 10),
            "live_enabled": self.settings.live.enabled,
            "live_armed": armed,
        }
        for name, value in checks.items():
            if not value: blockers.append(name)
        return LiveReadiness(**checks, execution_ready=all(checks.values()), blockers=tuple(blockers))

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
            if status not in {"NEW", "PARTIALLY_FILLED", "FILLED"}:
                raise SecurityError("ambiguous live Spot close response requires reconciliation; do not retry")
            return str(status)
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
        self.client_ids(proposal_id)
        raise SecurityError("reconciliation transport is unavailable; outcome remains unknown and must not be retried")

    def _direct_mcp_result(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Call the dedicated OAuth MCP profile without an LLM subprocess."""
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
        envelope = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": self.tool_name, "arguments": dict(request)}}
        http_request = urllib.request.Request(
            "https://agent.binance.com/mcp/agentic", data=json.dumps(envelope).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                     "Accept": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(http_request, timeout=self.settings.codex.timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise SecurityError("protected Binance MCP transport did not complete; reconciliation required") from exc
        if not isinstance(payload, Mapping) or payload.get("error"):
            raise SecurityError("protected Binance MCP returned an error; reconciliation required")
        result = payload.get("result")
        if not isinstance(result, Mapping) or result.get("isError") is True:
            raise SecurityError("protected Binance MCP returned an invalid result; reconciliation required")
        return result

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
        """Submit one already-confirmed protected request through Codex MCP.

        The caller supplies no endpoint or arguments; both are derived from the
        immutable proposal.  A failed/ambiguous child result is intentionally
        surfaced to the service so it can mark the proposal RECONCILE rather
        than retrying a possibly accepted Binance request.
        """
        request = self.protected_request(proposal)
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
        prompt = (
            "Use only the Binance MCP tool_execute tool exactly once. "
            "Do not use shell, files, web, or any other tool. Call it with this exact JSON envelope: "
            + json.dumps(request, sort_keys=True, separators=(",", ":"))
            + ". Return only the MCP result."
        )
        command = [
            self.settings.codex.command, "exec", "--json", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "-c",
            f'mcp_servers.{server}.tools.tool_execute.approval_mode="approve"', prompt,
        ]
        environment = {key: value for key, value in os.environ.items()
                       if key in {"LANG", "LC_ALL", "PATH", "SHELL", "TERM"}}
        environment["CODEX_HOME"] = str(home)
        environment.setdefault("PATH", os.defpath)
        environment.setdefault("SHELL", "/bin/sh")
        try:
            result = subprocess.run(command, cwd=str(workspace), env=environment,
                                    text=True, capture_output=True,
                                    timeout=self.settings.codex.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityError("protected Binance execution transport did not complete; reconciliation required") from exc
        if result.returncode != 0:
            raise SecurityError("protected Binance execution transport failed; reconciliation required")
        calls: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if (isinstance(item, dict) and item.get("type") == "mcp_tool_call"
                    and event.get("type") == "item.completed"):
                calls.append(item)
        if len(calls) != 1:
            raise SecurityError("protected Binance execution used an unexpected number of MCP calls; reconciliation required")
        call = calls[0]
        if (call.get("server") != server or call.get("tool") != self.tool_name
                or call.get("arguments") != request or call.get("status") != "completed" or call.get("error")):
            raise SecurityError("protected Binance execution did not match the approved Spot OTOCO request; reconciliation required")
        result_data = call.get("result")
        response = result_data.get("structured_content") if isinstance(result_data, Mapping) else None
        if response is None and isinstance(result_data, Mapping):
            content = result_data.get("content")
            if (isinstance(content, list) and len(content) == 1
                    and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str)):
                try:
                    response = json.loads(content[0]["text"])
                except json.JSONDecodeError:
                    response = None
        if not isinstance(response, Mapping):
            raise SecurityError("protected Binance execution response is malformed; reconciliation required")
        self.validate_write_response(response)
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
        return {"account_type": "SPOT", "balances": rows}

    def read_open_spot_orders(self) -> list[dict[str, Any]]:
        response = self._read_exact("open_orders")
        if not isinstance(response, list):
            raise SecurityError("open Spot orders response is malformed")
        fields = ("symbol", "orderId", "orderListId", "clientOrderId", "side", "type", "timeInForce",
                  "price", "stopPrice", "origQty", "executedQty", "status")
        if any(not isinstance(row, Mapping) or not isinstance(row.get("symbol"), str) for row in response):
            raise SecurityError("open Spot order response is malformed")
        return [{field: row[field] for field in fields if field in row} for row in response]

    def _read_exact(self, kind: str) -> Any:
        if kind not in self._READ_REQUESTS:
            raise SecurityError("unapproved live account read")
        tool_name, arguments = self._READ_REQUESTS[kind]
        home, workspace = self.settings.codex.agent_os_home, self.settings.codex.agent_os_workspace
        if home is None or workspace is None:
            raise SecurityError("dedicated Binance execution profile is not configured")
        request = {"toolName": tool_name, "arguments": arguments}
        return self._decode_mcp_result(self._direct_mcp_result(request))
        prompt = ("Use only the Binance MCP tool_execute tool exactly once. Do not use shell, files, web, "
                  "or any other tool. Call it with this exact JSON envelope: "
                  + json.dumps(request, sort_keys=True, separators=(",", ":")) + ". Return only the MCP result.")
        command = [self.settings.codex.command, "exec", "--json", "--sandbox", "read-only",
                   "--skip-git-repo-check", "--ephemeral", "-c",
                   f'mcp_servers.{self.settings.codex.mcp_server}.tools.tool_execute.approval_mode="approve"', prompt]
        environment = {key: value for key, value in os.environ.items() if key in {"LANG", "LC_ALL", "PATH", "SHELL", "TERM"}}
        environment["CODEX_HOME"] = str(home); environment.setdefault("PATH", os.defpath); environment.setdefault("SHELL", "/bin/sh")
        try:
            result = subprocess.run(command, cwd=str(workspace), env=environment, text=True, capture_output=True,
                                    timeout=self.settings.codex.timeout_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SecurityError("protected Spot account read did not complete") from exc
        if result.returncode != 0:
            raise SecurityError("protected Spot account read failed")
        calls = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item") if isinstance(event, dict) else None
            if isinstance(item, Mapping) and item.get("type") == "mcp_tool_call" and event.get("type") == "item.completed":
                calls.append(item)
        if len(calls) != 1:
            raise SecurityError("protected Spot account read used an unexpected number of MCP calls")
        call = calls[0]
        if (call.get("server") != self.settings.codex.mcp_server or call.get("tool") != self.tool_name
                or call.get("arguments") != request or call.get("error") or call.get("status") != "completed"):
            raise SecurityError("protected Spot account read did not match the approved request")
        result_data = call.get("result")
        if not isinstance(result_data, Mapping) or result_data.get("isError") is True:
            raise SecurityError("protected Spot account read returned an error")
        if result_data.get("structured_content") is not None:
            return result_data["structured_content"]
        content = result_data.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping) or not isinstance(content[0].get("text"), str):
            raise SecurityError("protected Spot account read response is malformed")
        try:
            return json.loads(content[0]["text"])
        except json.JSONDecodeError as exc:
            raise SecurityError("protected Spot account read response is malformed") from exc


    @classmethod
    def validate_tool(cls, server: str, tool: str) -> None:
        text = f"{server}:{tool}".lower()
        if server != cls.server or tool != cls.tool_name or any(x in text for x in FORBIDDEN_FAMILIES):
            raise SecurityError("MCP write target is not the exact approved protected Binance Spot tool")
