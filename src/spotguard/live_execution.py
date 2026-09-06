from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import os
import subprocess
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
    delegated_tool_name = "spot.orderList.place.otoco"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def freeze(self, proposal: Mapping[str, Any]) -> Mapping[str, Any]:
        canonical = dict(proposal.get("canonical", {}))
        if proposal.get("mode") != "live" or canonical.get("mode") != "live":
            raise SecurityError("LiveExecutionAdapter accepts LIVE proposals only")
        if canonical.get("product") != "SPOT" or canonical.get("side") != "BUY" or canonical.get("order_type") not in {"LIMIT", "LIMIT_MAKER"}:
            raise SecurityError("live intent must be an exact protected Spot LIMIT BUY")
        if canonical.get("symbol") not in self.settings.market.symbols:
            raise SecurityError("live symbol is not configured")
        if decimal_value(canonical.get("quote_amount"), "quote_amount") > self.settings.live.max_quote_per_entry_usdt:
            raise SecurityError("live quote amount exceeds the configured per-entry limit")
        return MappingProxyType(canonical)

    def protected_request(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """Build the only write shape RiskPilot permits, without sending it."""
        canonical = self.freeze(proposal)
        required = ("quantity", "entry_limit_price", "stop_reference", "take_profit_reference")
        if any(key not in canonical for key in required):
            raise SecurityError("live proposal is missing required TP/SL protection fields")
        quantity = decimal_value(canonical["quantity"], "quantity")
        entry = decimal_value(canonical["entry_limit_price"], "entry_limit_price")
        stop = decimal_value(canonical["stop_reference"], "stop_reference")
        target = decimal_value(canonical["take_profit_reference"], "take_profit_reference")
        if quantity <= 0 or stop <= 0 or target <= entry or stop >= entry:
            raise SecurityError("live TP/SL bracket is invalid")
        ids = self.client_ids(str(proposal.get("id", "")))
        return {
            "toolName": self.delegated_tool_name,
            "arguments": {
                "symbol": canonical["symbol"], "workingType": "LIMIT", "workingSide": "BUY",
                "workingPrice": format(entry, "f"), "workingQuantity": format(quantity, "f"),
                "workingTimeInForce": "GTC", "workingClientOrderId": ids["working_client_order_id"],
                "pendingSide": "SELL", "pendingQuantity": format(quantity, "f"),
                "pendingAboveType": "TAKE_PROFIT_LIMIT", "pendingAbovePrice": format(target, "f"),
                "pendingAboveStopPrice": format(target, "f"), "pendingAboveTimeInForce": "GTC",
                "pendingAboveClientOrderId": ids["pending_above_client_order_id"],
                "pendingBelowType": "STOP_LOSS_LIMIT", "pendingBelowStopPrice": format(stop, "f"),
                "pendingBelowPrice": format(stop * Decimal("0.999"), "f"),
                "pendingBelowTimeInForce": "GTC", "pendingBelowClientOrderId": ids["pending_below_client_order_id"],
                "listClientOrderId": ids["order_list_client_id"],
            },
        }

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
    def validate_write_response(response: Mapping[str, Any]) -> str:
        if not isinstance(response, Mapping) or not isinstance(response.get("orderListId"), int):
            raise SecurityError("malformed protected Spot write response")
        status = response.get("listStatusType")
        if status not in {"EXEC_STARTED", "ALL_DONE", "RESPONSE"}:
            raise SecurityError("ambiguous protected Spot response requires reconciliation; do not retry")
        return str(status)

    def reconcile(self, proposal_id: str) -> None:
        self.client_ids(proposal_id)
        raise SecurityError("reconciliation transport is unavailable; outcome remains unknown and must not be retried")

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
            if isinstance(item, dict) and item.get("type") == "mcp_tool_call":
                calls.append(item)
        if len(calls) != 1:
            raise SecurityError("protected Binance execution used an unexpected number of MCP calls; reconciliation required")
        call = calls[0]
        if (call.get("server") != server or call.get("tool") != self.tool_name
                or call.get("arguments") != request or call.get("status") != "completed" or call.get("error")):
            raise SecurityError("protected Binance execution did not match the approved Spot OTOCO request; reconciliation required")
        response = call.get("result", {}).get("structured_content") if isinstance(call.get("result"), Mapping) else None
        if not isinstance(response, Mapping):
            raise SecurityError("protected Binance execution response is malformed; reconciliation required")
        self.validate_write_response(response)
        return dict(response)

    @classmethod
    def validate_tool(cls, server: str, tool: str) -> None:
        text = f"{server}:{tool}".lower()
        if server != cls.server or tool != cls.tool_name or any(x in text for x in FORBIDDEN_FAMILIES):
            raise SecurityError("MCP write target is not the exact approved protected Binance Spot tool")
