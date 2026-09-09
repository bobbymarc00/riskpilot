from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..util import canonical_json


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class HardLimits:
    """Legacy absolute limits retained as an optional emergency backstop."""

    max_entry_notional: Decimal
    max_total_open_exposure: Decimal
    max_risk_per_position: Decimal
    max_aggregate_open_risk: Decimal
    max_daily_realized_loss: Decimal
    max_weekly_realized_loss: Decimal | None
    max_economic_positions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_entry_notional": _decimal_text(self.max_entry_notional),
            "max_total_open_exposure": _decimal_text(self.max_total_open_exposure),
            "max_risk_per_position": _decimal_text(self.max_risk_per_position),
            "max_aggregate_open_risk": _decimal_text(self.max_aggregate_open_risk),
            "max_daily_realized_loss": _decimal_text(self.max_daily_realized_loss),
            "max_weekly_realized_loss": _decimal_text(self.max_weekly_realized_loss),
            "max_economic_positions": self.max_economic_positions,
        }


@dataclass(frozen=True)
class EffectiveLimits:
    max_entry_notional: Decimal
    max_total_open_exposure: Decimal
    max_risk_per_position: Decimal
    max_aggregate_open_risk: Decimal
    max_daily_realized_loss: Decimal
    max_weekly_realized_loss: Decimal | None
    max_economic_positions: int
    required_reserve: Decimal
    effective_equity: Decimal
    equity_scale: Decimal
    calculation: str
    absolute_backstop_applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_entry_notional": _decimal_text(self.max_entry_notional),
            "max_total_open_exposure": _decimal_text(self.max_total_open_exposure),
            "max_risk_per_position": _decimal_text(self.max_risk_per_position),
            "max_aggregate_open_risk": _decimal_text(self.max_aggregate_open_risk),
            "max_daily_realized_loss": _decimal_text(self.max_daily_realized_loss),
            "max_weekly_realized_loss": _decimal_text(self.max_weekly_realized_loss),
            "max_economic_positions": self.max_economic_positions,
            "required_reserve": _decimal_text(self.required_reserve),
            "effective_equity": _decimal_text(self.effective_equity),
            "equity_scale": _decimal_text(self.equity_scale),
            "calculation": self.calculation,
            "absolute_backstop_applied": self.absolute_backstop_applied,
        }


def hard_limits_for(settings: Settings, mode: str) -> HardLimits:
    if mode == "paper":
        configured = settings.paper
        weekly: Decimal | None = None
    elif mode == "live":
        configured = settings.live
        weekly = configured.weekly_loss_cap_usdt
    else:
        raise ValueError("policy mode must be paper or live")
    return HardLimits(
        max_entry_notional=min(
            settings.risk.max_quote_per_trade, configured.max_quote_per_entry_usdt
        ),
        max_total_open_exposure=configured.max_open_exposure_usdt,
        max_risk_per_position=configured.max_risk_per_position_usdt,
        max_aggregate_open_risk=configured.max_aggregate_risk_usdt,
        max_daily_realized_loss=configured.daily_realized_loss_cap_usdt,
        max_weekly_realized_loss=weekly,
        max_economic_positions=configured.max_economic_positions,
    )


def uses_absolute_backstop(settings: Settings) -> bool:
    policy = settings.sizing_policy
    return not policy.percentage_based or policy.absolute_usd_backstop_enabled


def absolute_entry_ceiling(settings: Settings, mode: str) -> Decimal | None:
    """Return a USD ceiling only for legacy/v1/backstop semantics."""

    if uses_absolute_backstop(settings):
        return hard_limits_for(settings, mode).max_entry_notional
    return None


def absolute_daily_quote_ceiling(settings: Settings) -> Decimal | None:
    """Legacy turnover quota is not a primary v2 equity risk limit."""

    return settings.risk.max_daily_quote if uses_absolute_backstop(settings) else None


def effective_reserve(
    settings: Settings, mode: str, equity: Decimal | None = None
) -> Decimal:
    """Calculate reserved free quote; it never counts as spendable capital."""

    if mode not in {"paper", "live"}:
        raise ValueError("policy mode must be paper or live")
    policy = settings.sizing_policy
    if policy.percentage_based:
        if equity is None:
            raise ValueError("equity is required for percentage-based reserve")
        assert policy.capital is not None
        # Schema v2 reserve is purely percentage-based. Absolute safety caps
        # are ceilings for maximum allowances; they must not silently turn the
        # legacy LIVE reserve into a minimum floor. A future absolute reserve
        # floor requires its own explicit, versioned setting.
        return equity * policy.capital.min_free_reserve_pct
    if policy.enabled:
        return policy.reserve_quote_amount
    return settings.live.min_free_reserve_usdt if mode == "live" else Decimal("0")


def limits_for(
    settings: Settings,
    mode: str,
    equity: Decimal,
    *,
    effective_equity: Decimal | None = None,
) -> tuple[HardLimits, EffectiveLimits]:
    """Return the canonical limits shared by PAPER and LIVE.

    ``effective_equity`` supports execution-time conservative equity
    ``min(equity_at_proposal, current_equity)`` while current free quote remains
    the only source of buying power.
    """

    if not equity.is_finite() or equity < 0:
        raise ValueError("equity must be finite and non-negative")
    basis = equity if effective_equity is None else effective_equity
    if not basis.is_finite() or basis < 0 or basis > equity:
        raise ValueError(
            "effective equity must be finite, non-negative, and no greater than current equity"
        )
    hard = hard_limits_for(settings, mode)
    policy = settings.sizing_policy

    if policy.percentage_based:
        assert policy.equity_risk is not None
        assert policy.capital is not None
        assert policy.operations is not None
        assert policy.reference_equity_usdt is not None
        risk = policy.equity_risk
        capital = policy.capital

        def capped(value: Decimal, ceiling: Decimal | None) -> Decimal:
            if policy.absolute_usd_backstop_enabled and ceiling is not None:
                return min(value, ceiling)
            return value

        return hard, EffectiveLimits(
            max_entry_notional=capped(
                basis * capital.max_position_pct, hard.max_entry_notional
            ),
            max_total_open_exposure=capped(
                basis * capital.max_total_exposure_pct,
                hard.max_total_open_exposure,
            ),
            max_risk_per_position=capped(
                basis * risk.risk_per_trade_pct, hard.max_risk_per_position
            ),
            max_aggregate_open_risk=capped(
                basis * risk.max_aggregate_open_risk_pct,
                hard.max_aggregate_open_risk,
            ),
            max_daily_realized_loss=capped(
                basis * risk.daily_realized_loss_pct,
                hard.max_daily_realized_loss,
            ),
            max_weekly_realized_loss=capped(
                basis * risk.weekly_realized_loss_pct,
                hard.max_weekly_realized_loss,
            ),
            max_economic_positions=policy.operations.max_open_positions,
            required_reserve=effective_reserve(settings, mode, basis),
            effective_equity=basis,
            # Diagnostic scale relative to the explicit audit/reference
            # equity. Percentage limits themselves use ``basis`` directly.
            equity_scale=basis / policy.reference_equity_usdt,
            calculation="equity_percentage_risk",
            absolute_backstop_applied=policy.absolute_usd_backstop_enabled,
        )

    if policy.enabled:
        assert policy.reference_equity_usdt is not None
        scale = min(Decimal("1"), basis / policy.reference_equity_usdt)
        calculation = "linear_to_hard_cap"
    else:
        scale = Decimal("1")
        calculation = "legacy_absolute_limits"

    def scaled(value: Decimal) -> Decimal:
        return min(value, value * scale)

    return hard, EffectiveLimits(
        max_entry_notional=scaled(hard.max_entry_notional),
        max_total_open_exposure=scaled(hard.max_total_open_exposure),
        max_risk_per_position=scaled(hard.max_risk_per_position),
        max_aggregate_open_risk=scaled(hard.max_aggregate_open_risk),
        max_daily_realized_loss=scaled(hard.max_daily_realized_loss),
        max_weekly_realized_loss=(
            scaled(hard.max_weekly_realized_loss)
            if hard.max_weekly_realized_loss is not None
            else None
        ),
        max_economic_positions=hard.max_economic_positions,
        required_reserve=effective_reserve(settings, mode, basis),
        effective_equity=basis,
        equity_scale=scale,
        calculation=calculation,
        absolute_backstop_applied=True,
    )


def policy_descriptor(settings: Settings, mode: str) -> dict[str, Any]:
    hard = hard_limits_for(settings, mode).to_dict()
    return {
        "config_schema_version": settings.version,
        "policy_schema": f"riskpilot.sizing.v{settings.sizing_policy.schema_version}",
        "mode": mode,
        "sizing_policy": settings.sizing_policy.to_dict(),
        "absolute_safety_caps": {
            "enabled": settings.sizing_policy.absolute_usd_backstop_enabled,
            "limits": hard,
        },
        # Kept for consumers of the phase-1 draft snapshot.
        "hard_limits": hard,
    }


def policy_fingerprint(settings: Settings, mode: str) -> str:
    return hashlib.sha256(
        canonical_json(policy_descriptor(settings, mode)).encode()
    ).hexdigest()
