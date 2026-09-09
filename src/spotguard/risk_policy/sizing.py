from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from .evaluator import PolicyContext, PolicyReason


def _text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class SizingDecision:
    accepted: bool
    reason_details: tuple[PolicyReason, ...]
    requested_notional: Decimal | None
    calculated_notional: Decimal
    stop_distance_pct: Decimal
    risk_budget: Decimal
    risk_based_notional: Decimal
    max_position_notional: Decimal
    remaining_position_capacity: Decimal
    remaining_exposure_capacity: Decimal
    remaining_risk_capacity: Decimal
    risk_capacity_notional: Decimal
    available_capital_after_reserve: Decimal
    fee_buffer_rate: Decimal
    estimated_fee_buffer: Decimal
    maximum_safe_notional: Decimal
    expected_risk: Decimal

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(item.render() for item in self.reason_details)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.reason_details)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason_codes": list(self.reason_codes),
            "reasons": list(self.reasons),
            "reason_details": [item.to_dict() for item in self.reason_details],
            "requested_notional": _text(self.requested_notional),
            "calculated_notional": _text(self.calculated_notional),
            "stop_distance_pct": _text(self.stop_distance_pct),
            "risk_budget": _text(self.risk_budget),
            "risk_based_notional": _text(self.risk_based_notional),
            "max_position_notional": _text(self.max_position_notional),
            "remaining_position_capacity": _text(
                self.remaining_position_capacity
            ),
            "remaining_exposure_capacity": _text(
                self.remaining_exposure_capacity
            ),
            "remaining_risk_capacity": _text(self.remaining_risk_capacity),
            "risk_capacity_notional": _text(self.risk_capacity_notional),
            "available_capital_after_reserve": _text(
                self.available_capital_after_reserve
            ),
            "fee_buffer_rate": _text(self.fee_buffer_rate),
            "estimated_fee_buffer": _text(self.estimated_fee_buffer),
            "maximum_safe_notional": _text(self.maximum_safe_notional),
            "expected_risk": _text(self.expected_risk),
        }


def size_entry(
    context: PolicyContext,
    *,
    entry_price: Decimal,
    stop_price: Decimal,
    existing_position_exposure: Decimal = Decimal("0"),
    existing_position_risk: Decimal = Decimal("0"),
    requested_notional: Decimal | None = None,
    fee_buffer_rate: Decimal = Decimal("0"),
) -> SizingDecision:
    """Calculate risk-derived size before proposal creation.

    An explicit amount is never mutated: it is accepted exactly or rejected.
    With no explicit amount, the function returns the largest safe amount for
    the immutable proposal, rounded down to eight quote decimals.
    """

    numbers = (
        entry_price,
        stop_price,
        existing_position_exposure,
        existing_position_risk,
        fee_buffer_rate,
    )
    if any(not value.is_finite() for value in numbers):
        raise ValueError("sizing inputs must be finite")
    if entry_price <= 0 or stop_price <= 0 or stop_price >= entry_price:
        raise ValueError("structural stop must satisfy 0 < stop < entry")
    if min(existing_position_exposure, existing_position_risk, fee_buffer_rate) < 0:
        raise ValueError("existing usage and fee buffer cannot be negative")
    if requested_notional is not None and (
        not requested_notional.is_finite() or requested_notional <= 0
    ):
        raise ValueError("requested notional must be finite and positive")

    limits = context.effective_limits
    stop_fraction = (entry_price - stop_price) / entry_price
    risk_budget = limits.max_risk_per_position
    risk_based = risk_budget / stop_fraction
    position_capacity = max(
        Decimal("0"), limits.max_entry_notional - existing_position_exposure
    )
    exposure_capacity = max(
        Decimal("0"), limits.max_total_open_exposure - context.usage.open_exposure
    )
    remaining_risk = max(
        Decimal("0"),
        limits.max_aggregate_open_risk - context.usage.aggregate_open_risk,
    )
    position_risk_remaining = max(
        Decimal("0"), risk_budget - existing_position_risk
    )
    usable_risk = min(remaining_risk, position_risk_remaining)
    risk_capacity_notional = usable_risk / stop_fraction
    # available_buying_power is free quote minus reserve. Solve notional +
    # fee_rate*notional <= buying power rather than subtracting an estimate
    # computed from an unsafe pre-clamp amount.
    capital_capacity = context.equity.available_buying_power / (
        Decimal("1") + fee_buffer_rate
    )
    safe = min(
        risk_based,
        limits.max_entry_notional,
        position_capacity,
        exposure_capacity,
        risk_capacity_notional,
        capital_capacity,
    )
    safe = max(Decimal("0"), safe).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    selected = requested_notional if requested_notional is not None else safe
    details: list[PolicyReason] = []
    if selected <= 0:
        details.append(
            PolicyReason(
                "REVALIDATION_FAILED", "no positive risk-derived capacity remains"
            )
        )
    elif selected > safe:
        candidates = (
            (risk_based, "MAX_POSITION_EXCEEDED", "risk-per-trade budget"),
            (position_capacity, "MAX_POSITION_EXCEEDED", "single-position concentration capacity"),
            (exposure_capacity, "MAX_TOTAL_EXPOSURE_EXCEEDED", "total exposure capacity"),
            (
                remaining_risk / stop_fraction,
                "MAX_AGGREGATE_RISK_EXCEEDED",
                "aggregate open-risk capacity",
            ),
            (
                position_risk_remaining / stop_fraction,
                "MAX_POSITION_EXCEEDED",
                "economic-position risk capacity",
            ),
            (capital_capacity, "MIN_FREE_RESERVE_VIOLATION", "free quote after reserve and fee buffer"),
        )
        limiting = min(candidates, key=lambda item: item[0])
        details.append(
            PolicyReason(
                limiting[1],
                f"exact requested notional {selected:f} exceeds safe maximum {safe:f}; limiting gate is {limiting[2]}",
            )
        )
        if selected > context.equity.free_quote:
            details.append(
                PolicyReason(
                    "INSUFFICIENT_FREE_BALANCE",
                    f"exact requested notional {selected:f} exceeds free quote balance {context.equity.free_quote:f}",
                )
            )
    expected_risk = selected * stop_fraction if selected > 0 else Decimal("0")
    fee = selected * fee_buffer_rate if selected > 0 else Decimal("0")
    return SizingDecision(
        accepted=not details,
        reason_details=tuple(details),
        requested_notional=requested_notional,
        calculated_notional=selected,
        stop_distance_pct=stop_fraction,
        risk_budget=risk_budget,
        risk_based_notional=risk_based,
        max_position_notional=limits.max_entry_notional,
        remaining_position_capacity=position_capacity,
        remaining_exposure_capacity=exposure_capacity,
        remaining_risk_capacity=remaining_risk,
        risk_capacity_notional=risk_capacity_notional,
        available_capital_after_reserve=context.equity.available_buying_power,
        fee_buffer_rate=fee_buffer_rate,
        estimated_fee_buffer=fee,
        maximum_safe_notional=safe,
        expected_risk=expected_risk,
    )
