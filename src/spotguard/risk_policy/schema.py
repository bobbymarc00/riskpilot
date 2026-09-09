from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


class SizingPolicyConfigError(ValueError):
    pass


@dataclass(frozen=True)
class EquityRiskSettings:
    risk_per_trade_pct: Decimal
    max_aggregate_open_risk_pct: Decimal
    daily_realized_loss_pct: Decimal
    weekly_realized_loss_pct: Decimal

    def to_dict(self) -> dict[str, str]:
        return {key: format(getattr(self, key), "f") for key in (
            "risk_per_trade_pct", "max_aggregate_open_risk_pct",
            "daily_realized_loss_pct", "weekly_realized_loss_pct",
        )}


@dataclass(frozen=True)
class CapitalAllocationSettings:
    max_position_pct: Decimal
    max_total_exposure_pct: Decimal
    min_free_reserve_pct: Decimal

    def to_dict(self) -> dict[str, str]:
        return {key: format(getattr(self, key), "f") for key in (
            "max_position_pct", "max_total_exposure_pct", "min_free_reserve_pct",
        )}


@dataclass(frozen=True)
class OperationsPolicySettings:
    max_open_positions: int
    max_pending_live_proposals: int

    def to_dict(self) -> dict[str, int]:
        return {
            "max_open_positions": self.max_open_positions,
            "max_pending_live_proposals": self.max_pending_live_proposals,
        }


@dataclass(frozen=True)
class ExecutionPolicySettings:
    max_equity_drift_pct: Decimal

    def to_dict(self) -> dict[str, str]:
        return {"max_equity_drift_pct": format(self.max_equity_drift_pct, "f")}


@dataclass(frozen=True)
class SizingPolicySettings:
    """Versioned equity policy with a strict legacy fallback.

    Schema v1 retains the earlier linear, down-only policy. Schema v2 is
    percentage-based and scales both down and up. Existing USD limits constrain
    v2 only when its optional emergency backstop is enabled.
    """

    configured: bool
    enabled: bool
    schema_version: int
    capital_basis: str
    quote_asset: str
    scaling_model: str
    absolute_usd_backstop_enabled: bool
    reference_equity_usdt: Decimal | None = None
    reserve_quote_amount: Decimal = Decimal("0")
    equity_risk: EquityRiskSettings | None = None
    capital: CapitalAllocationSettings | None = None
    operations: OperationsPolicySettings | None = None
    execution: ExecutionPolicySettings | None = None

    @property
    def percentage_based(self) -> bool:
        return self.enabled and self.schema_version == 2

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "configured": self.configured,
            "enabled": self.enabled,
            "schema_version": self.schema_version,
            "capital_basis": self.capital_basis,
            "quote_asset": self.quote_asset,
            "scaling_model": self.scaling_model,
            "absolute_usd_backstop_enabled": self.absolute_usd_backstop_enabled,
            "fallback": None if self.configured else "legacy_absolute_limits",
        }
        if self.schema_version == 1:
            result.update({
                "reference_equity_usdt": format(
                    self.reference_equity_usdt or Decimal("0"), "f"
                ),
                "reserve_quote_amount": format(self.reserve_quote_amount, "f"),
            })
        else:
            result.update({
                # Audit/migration anchor only. Percentage limits do not use it
                # as a ceiling or denominator.
                "reference_equity_usdt": format(
                    self.reference_equity_usdt or Decimal("0"), "f"
                ),
                "risk": self.equity_risk.to_dict() if self.equity_risk else None,
                "capital": self.capital.to_dict() if self.capital else None,
                "operations": self.operations.to_dict() if self.operations else None,
                "execution": self.execution.to_dict() if self.execution else None,
            })
        return result


def _decimal(
    value: Any, field: str, *, allow_zero: bool = False, at_most_one: bool = False
) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SizingPolicyConfigError(f"{field} must be a decimal") from exc
    if not result.is_finite() or result < 0 or (not allow_zero and result == 0):
        qualifier = "non-negative" if allow_zero else "greater than zero"
        raise SizingPolicyConfigError(f"{field} must be finite and {qualifier}")
    if at_most_one and result > 1:
        raise SizingPolicyConfigError(f"{field} must be at most 1")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SizingPolicyConfigError(f"{field} must be an object")
    return value


def _required_fraction(value: Mapping[str, Any], key: str, group: str) -> Decimal:
    if key not in value:
        raise SizingPolicyConfigError(f"{group}.{key} is required")
    return _decimal(value[key], f"{group}.{key}", at_most_one=True)


def _required_int(
    value: Mapping[str, Any], key: str, group: str, *, maximum: int = 100
) -> int:
    raw = value.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= maximum:
        raise SizingPolicyConfigError(
            f"{group}.{key} must be an integer between 1 and {maximum}"
        )
    return raw


def load_sizing_policy(
    value: Any,
    *,
    quote_asset: str,
    legacy_reference_equity: Decimal,
    risk_value: Any = None,
    capital_value: Any = None,
    operations_value: Any = None,
    execution_value: Any = None,
    absolute_safety_caps_value: Any = None,
) -> SizingPolicySettings:
    """Load schema v1/v2, or preserve legacy absolute behavior when omitted."""

    if value is None:
        return SizingPolicySettings(
            configured=False,
            enabled=False,
            schema_version=1,
            capital_basis="mark_to_market_equity",
            quote_asset=quote_asset,
            scaling_model="legacy_absolute_limits",
            absolute_usd_backstop_enabled=True,
            reference_equity_usdt=_decimal(
                legacy_reference_equity, "paper.initial_balance_usdt"
            ),
        )
    policy = _mapping(value, "sizing_policy")
    enabled = policy.get("enabled")
    if not isinstance(enabled, bool):
        raise SizingPolicyConfigError("sizing_policy.enabled must be true or false")
    schema_version = policy.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in {1, 2}:
        raise SizingPolicyConfigError("sizing_policy.schema_version must be 1 or 2")
    capital_basis = str(policy.get("capital_basis", ""))
    if capital_basis != "mark_to_market_equity":
        raise SizingPolicyConfigError(
            "sizing_policy.capital_basis must be mark_to_market_equity"
        )
    configured_quote = str(policy.get("quote_asset", "")).upper()
    if configured_quote != quote_asset:
        raise SizingPolicyConfigError(
            "sizing_policy.quote_asset must match risk.quote_asset"
        )

    if schema_version == 1:
        if str(policy.get("scaling_model", "")) != "linear_to_hard_cap":
            raise SizingPolicyConfigError(
                "sizing_policy.scaling_model must be linear_to_hard_cap for schema 1"
            )
        if "reference_equity_usdt" not in policy:
            raise SizingPolicyConfigError(
                "sizing_policy.reference_equity_usdt is required for schema 1"
            )
        return SizingPolicySettings(
            configured=True,
            enabled=enabled,
            schema_version=1,
            capital_basis=capital_basis,
            quote_asset=configured_quote,
            scaling_model="linear_to_hard_cap",
            absolute_usd_backstop_enabled=True,
            reference_equity_usdt=_decimal(
                policy["reference_equity_usdt"],
                "sizing_policy.reference_equity_usdt",
            ),
            reserve_quote_amount=_decimal(
                policy.get("reserve_quote_amount", "0"),
                "sizing_policy.reserve_quote_amount",
                allow_zero=True,
            ),
        )

    if str(policy.get("scaling_model", "")) != "equity_percentage_risk":
        raise SizingPolicyConfigError(
            "sizing_policy.scaling_model must be equity_percentage_risk for schema 2"
        )
    if absolute_safety_caps_value is not None:
        caps_group = _mapping(absolute_safety_caps_value, "absolute_safety_caps")
        backstop = caps_group.get("enabled")
        if "absolute_usd_backstop_enabled" in policy and (
            isinstance(backstop, bool)
            and policy["absolute_usd_backstop_enabled"] != backstop
        ):
            raise SizingPolicyConfigError(
                "sizing_policy and absolute_safety_caps backstop flags conflict"
            )
    else:
        # Accepted for the short-lived schema-v2 preview; new configs use the
        # separate absolute_safety_caps group.
        backstop = policy.get("absolute_usd_backstop_enabled", False)
    if not isinstance(backstop, bool):
        raise SizingPolicyConfigError(
            "absolute_safety_caps.enabled must be true or false"
        )
    risk_group = _mapping(risk_value, "risk")
    capital_group = _mapping(capital_value, "capital")
    operations_group = _mapping(operations_value, "operations")
    execution_group = _mapping(execution_value, "execution")
    equity_risk = EquityRiskSettings(
        risk_per_trade_pct=_required_fraction(
            risk_group, "risk_per_trade_pct", "risk"
        ),
        max_aggregate_open_risk_pct=_required_fraction(
            risk_group, "max_aggregate_open_risk_pct", "risk"
        ),
        daily_realized_loss_pct=_required_fraction(
            risk_group, "daily_realized_loss_pct", "risk"
        ),
        weekly_realized_loss_pct=_required_fraction(
            risk_group, "weekly_realized_loss_pct", "risk"
        ),
    )
    capital = CapitalAllocationSettings(
        max_position_pct=_required_fraction(
            capital_group, "max_position_pct", "capital"
        ),
        max_total_exposure_pct=_required_fraction(
            capital_group, "max_total_exposure_pct", "capital"
        ),
        min_free_reserve_pct=_required_fraction(
            capital_group, "min_free_reserve_pct", "capital"
        ),
    )
    operations = OperationsPolicySettings(
        max_open_positions=_required_int(
            operations_group, "max_open_positions", "operations"
        ),
        max_pending_live_proposals=_required_int(
            operations_group, "max_pending_live_proposals", "operations", maximum=20
        ),
    )
    execution = ExecutionPolicySettings(
        max_equity_drift_pct=_required_fraction(
            execution_group, "max_equity_drift_pct", "execution"
        )
    )
    if equity_risk.max_aggregate_open_risk_pct < equity_risk.risk_per_trade_pct:
        raise SizingPolicyConfigError(
            "risk.max_aggregate_open_risk_pct must be at least risk.risk_per_trade_pct"
        )
    if equity_risk.weekly_realized_loss_pct < equity_risk.daily_realized_loss_pct:
        raise SizingPolicyConfigError(
            "risk.weekly_realized_loss_pct must be at least risk.daily_realized_loss_pct"
        )
    if capital.max_total_exposure_pct < capital.max_position_pct:
        raise SizingPolicyConfigError(
            "capital.max_total_exposure_pct must be at least capital.max_position_pct"
        )
    return SizingPolicySettings(
        configured=True,
        enabled=enabled,
        schema_version=2,
        capital_basis=capital_basis,
        quote_asset=configured_quote,
        scaling_model="equity_percentage_risk",
        absolute_usd_backstop_enabled=backstop,
        reference_equity_usdt=_decimal(
            policy.get("reference_equity_usdt", legacy_reference_equity),
            "sizing_policy.reference_equity_usdt",
        ),
        equity_risk=equity_risk,
        capital=capital,
        operations=operations,
        execution=execution,
    )
