from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .capital import EquitySnapshot, UsageSnapshot
from .limits import EffectiveLimits, HardLimits


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


@dataclass(frozen=True)
class PolicyContext:
    equity: EquitySnapshot
    usage: UsageSnapshot
    hard_limits: HardLimits
    effective_limits: EffectiveLimits


@dataclass(frozen=True)
class PolicyReason:
    code: str
    explanation: str

    def render(self) -> str:
        return f"{self.code}: {self.explanation}"

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "explanation": self.explanation}


@dataclass(frozen=True)
class EvaluationResult:
    accepted: bool
    reason_details: tuple[PolicyReason, ...]
    requested_notional: Decimal | None
    projected_exposure: Decimal
    projected_position_exposure: Decimal | None
    projected_position_risk: Decimal | None
    projected_aggregate_risk: Decimal
    remaining_exposure: Decimal
    remaining_position_capacity: Decimal
    remaining_aggregate_risk: Decimal
    remaining_buying_power: Decimal

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
            "requested_notional": _decimal_text(self.requested_notional),
            "projected_exposure": _decimal_text(self.projected_exposure),
            "projected_position_exposure": _decimal_text(
                self.projected_position_exposure
            ),
            "projected_position_risk": _decimal_text(self.projected_position_risk),
            "projected_aggregate_risk": _decimal_text(self.projected_aggregate_risk),
            "remaining_exposure": _decimal_text(self.remaining_exposure),
            "remaining_position_capacity": _decimal_text(
                self.remaining_position_capacity
            ),
            "remaining_aggregate_risk": _decimal_text(
                self.remaining_aggregate_risk
            ),
            "remaining_buying_power": _decimal_text(self.remaining_buying_power),
        }


def evaluate_entry(
    context: PolicyContext,
    *,
    requested_notional: Decimal | None = None,
    projected_exposure: Decimal | None = None,
    projected_position_exposure: Decimal | None = None,
    projected_position_risk: Decimal | None = None,
    projected_aggregate_risk: Decimal | None = None,
    resulting_economic_positions: int | None = None,
    estimated_fee: Decimal = Decimal("0"),
) -> EvaluationResult:
    """Apply one fail-closed evaluator to already verified PAPER/LIVE state."""

    usage = context.usage
    limits = context.effective_limits
    details: list[PolicyReason] = []

    def reject(code: str, explanation: str) -> None:
        if code not in {item.code for item in details}:
            details.append(PolicyReason(code, explanation))

    exposure = (
        projected_exposure if projected_exposure is not None else usage.open_exposure
    )
    aggregate = (
        projected_aggregate_risk
        if projected_aggregate_risk is not None
        else usage.aggregate_open_risk
    )
    if requested_notional is not None:
        if not requested_notional.is_finite() or requested_notional <= 0:
            reject("REVALIDATION_FAILED", "requested notional must be finite and positive")
        if requested_notional > limits.max_entry_notional:
            reject(
                "MAX_POSITION_EXCEEDED",
                f"requested notional {requested_notional:f} exceeds effective single-position allowance {limits.max_entry_notional:f} {context.equity.quote_asset}",
            )
        spend_with_fee = requested_notional + estimated_fee
        if spend_with_fee > context.equity.available_buying_power:
            reject(
                "MIN_FREE_RESERVE_VIOLATION",
                f"requested notional plus fee {spend_with_fee:f} exceeds available buying power {context.equity.available_buying_power:f} {context.equity.quote_asset} after reserve",
            )
            if requested_notional > context.equity.free_quote:
                reject(
                    "INSUFFICIENT_FREE_BALANCE",
                    f"requested notional {requested_notional:f} exceeds free quote balance {context.equity.free_quote:f} {context.equity.quote_asset}",
                )
    if (
        projected_position_exposure is not None
        and projected_position_exposure > limits.max_entry_notional
    ):
        reject(
            "MAX_POSITION_EXCEEDED",
            f"projected economic-position exposure {projected_position_exposure:f} exceeds effective limit {limits.max_entry_notional:f} {context.equity.quote_asset}",
        )
    if exposure > limits.max_total_open_exposure:
        reject(
            "MAX_TOTAL_EXPOSURE_EXCEEDED",
            f"projected exposure {exposure:f} exceeds effective exposure limit {limits.max_total_open_exposure:f} {context.equity.quote_asset}",
        )
    if (
        projected_position_risk is not None
        and projected_position_risk > limits.max_risk_per_position
    ):
        reject(
            "MAX_POSITION_EXCEEDED",
            f"projected position risk {projected_position_risk:f} exceeds effective per-position risk limit {limits.max_risk_per_position:f} {context.equity.quote_asset}",
        )
    if aggregate > limits.max_aggregate_open_risk:
        reject(
            "MAX_AGGREGATE_RISK_EXCEEDED",
            f"projected aggregate risk {aggregate:f} exceeds effective aggregate risk limit {limits.max_aggregate_open_risk:f} {context.equity.quote_asset}",
        )
    elif (
        requested_notional is not None
        and projected_aggregate_risk is None
        and usage.aggregate_open_risk >= limits.max_aggregate_open_risk
    ):
        reject("MAX_AGGREGATE_RISK_EXCEEDED", "aggregate open risk capacity is exhausted")
    if usage.daily_realized_loss >= limits.max_daily_realized_loss:
        reject(
            "DAILY_LOSS_LIMIT_REACHED",
            f"daily realized loss {usage.daily_realized_loss:f} has exhausted effective limit {limits.max_daily_realized_loss:f} {context.equity.quote_asset}",
        )
    if (
        limits.max_weekly_realized_loss is not None
        and usage.weekly_realized_loss >= limits.max_weekly_realized_loss
    ):
        reject(
            "WEEKLY_LOSS_LIMIT_REACHED",
            f"weekly realized loss {usage.weekly_realized_loss:f} has exhausted effective limit {limits.max_weekly_realized_loss:f} {context.equity.quote_asset}",
        )
    if (
        resulting_economic_positions is not None
        and resulting_economic_positions > limits.max_economic_positions
    ):
        reject(
            "MAX_OPEN_POSITIONS_REACHED",
            f"economic position limit reached ({limits.max_economic_positions})",
        )
    return EvaluationResult(
        accepted=not details,
        reason_details=tuple(details),
        requested_notional=requested_notional,
        projected_exposure=exposure,
        projected_position_exposure=projected_position_exposure,
        projected_position_risk=projected_position_risk,
        projected_aggregate_risk=aggregate,
        remaining_exposure=max(
            Decimal("0"), limits.max_total_open_exposure - usage.open_exposure
        ),
        remaining_position_capacity=max(
            Decimal("0"),
            limits.max_entry_notional
            - (
                (projected_position_exposure - requested_notional)
                if projected_position_exposure is not None
                and requested_notional is not None
                else Decimal("0")
            ),
        ),
        remaining_aggregate_risk=max(
            Decimal("0"), limits.max_aggregate_open_risk - usage.aggregate_open_risk
        ),
        remaining_buying_power=max(
            Decimal("0"),
            context.equity.available_buying_power
            - (requested_notional or Decimal("0"))
            - estimated_fee,
        ),
    )
