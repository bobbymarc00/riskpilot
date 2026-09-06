from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Mapping

from .config import Settings
from .security import SecurityError
from .util import decimal_value


BINANCE_MCP_SERVER = "binance-mcp-server"
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
    tool_name: str | None = None

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

    def readiness(self, *, connected: bool, armed: bool, symbol_flags_verified: bool = False) -> LiveReadiness:
        blockers = []
        checks = {
            "binance_mcp_connected": connected and self.settings.codex.mcp_server == self.server,
            "agentic_account_accessible": False,
            "account_scope_available": False,
            "spot_trade_scope_available": False,
            "exact_spot_write_schema_verified": False,
            "protective_order_list_verified": False,
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

    def execute(self, proposal: Mapping[str, Any]) -> None:
        self.freeze(proposal)
        raise SecurityError("live execution is unavailable: exact protected Binance MCP OPO/OCO write schema and confirmation binding are unverified")

    @classmethod
    def validate_tool(cls, server: str, tool: str) -> None:
        text = f"{server}:{tool}".lower()
        if server != cls.server or cls.tool_name is None or tool != cls.tool_name or any(x in text for x in FORBIDDEN_FAMILIES):
            raise SecurityError("MCP write target is not the exact approved protected Binance Spot tool")
