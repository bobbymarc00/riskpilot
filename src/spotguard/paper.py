from __future__ import annotations

from decimal import Decimal
from typing import Any

from .config import Settings
from .policy import PolicyError
from .util import decimal_string, decimal_value


BRACKET_RR_TOLERANCE = Decimal("0.0001")


def validate_long_bracket(entry: Decimal, stop: Decimal, target: Decimal, reward_risk: Decimal, *, bid: Decimal | None = None, ask: Decimal | None = None, tolerance: Decimal = BRACKET_RR_TOLERANCE) -> Decimal:
    """Validate a non-trailing Spot BUY bracket and return its downside distance."""
    if min(entry, stop, target, reward_risk) <= 0:
        raise PolicyError("PAPER bracket values and reward/risk must be positive")
    downside = entry - stop
    if downside <= 0:
        raise PolicyError("invalid PAPER bracket: stop must be below aggregate average entry")
    if target <= entry:
        raise PolicyError("invalid PAPER bracket: take-profit must be above aggregate average entry")
    if bid is not None and (bid <= 0 or stop >= bid):
        raise PolicyError("invalid PAPER bracket: stop must be below the fresh executable bid")
    if ask is not None and (ask <= 0 or target <= ask):
        raise PolicyError("invalid PAPER bracket: take-profit must be above the fresh executable ask")
    actual = (target - entry) / downside
    if actual <= 0 or abs(actual - reward_risk) > tolerance:
        raise PolicyError(f"invalid PAPER bracket: reward/risk {actual:f} does not match configured {reward_risk:f}")
    return downside


def build_fill_risk(
    settings: Settings,
    proposal: dict[str, Any],
    fill_price: Decimal,
    gross_quantity: Decimal,
) -> dict[str, str]:
    if min(fill_price, gross_quantity) <= 0:
        raise PolicyError("paper fill price and quantity must be positive")
    entry_reference = decimal_value(proposal["entry_reference"], "entry_reference")
    approved_stop = decimal_value(proposal["stop_reference"], "stop_reference")
    approved_distance = entry_reference - approved_stop
    if approved_distance <= 0:
        raise PolicyError("approved proposal risk distance is invalid")
    fee_rate = settings.risk.paper_fee_pct / Decimal("100")
    slippage_rate = settings.paper.slippage_pct / Decimal("100")
    entry_fee_base = gross_quantity * fee_rate
    net_quantity = gross_quantity - entry_fee_base
    quote_spent = gross_quantity * fill_price
    stop = fill_price - approved_distance
    if stop <= 0 or net_quantity <= 0:
        raise PolicyError("fill-based stop or net quantity is invalid")

    stop_execution = stop * (Decimal("1") - slippage_rate)
    stop_gross = net_quantity * stop_execution
    stop_fee = stop_gross * fee_rate
    stop_net = stop_gross - stop_fee
    # This is the worst-case stop loss: the entry fee reduces acquired base,
    # the stop is executed after configured adverse slippage, and the exit fee
    # is deducted from proceeds.  Presentation must label it accordingly.
    risk_amount = quote_spent - stop_net
    if risk_amount <= 0 or risk_amount > settings.paper.max_risk_per_trade_usdt:
        raise PolicyError("fill-based paper risk exceeds the per-trade risk cap")

    minimum_rr = settings.risk.min_reward_risk
    # Keep the advertised geometric R:R exact. Fees/slippage remain explicit
    # accounting fields and must not silently stretch the target distance.
    target = fill_price + approved_distance * minimum_rr
    validate_long_bracket(fill_price, stop, target, minimum_rr, bid=fill_price, ask=fill_price)
    target_execution = target * (Decimal("1") - slippage_rate)
    target_gross = net_quantity * target_execution
    target_fee = target_gross * fee_rate
    target_net = target_gross - target_fee
    reward = target_net - quote_spent
    net_rr = reward / risk_amount
    return {
        "average_fill_price": decimal_string(fill_price, 8),
        "gross_base_quantity": decimal_string(gross_quantity, 12),
        "entry_fee_base": decimal_string(entry_fee_base, 12),
        "net_base_quantity": decimal_string(net_quantity, 12),
        "quote_spent": decimal_string(quote_spent, 12),
        "final_stop": decimal_string(stop, 8),
        "final_target": decimal_string(target, 8),
        "risk_amount": decimal_string(risk_amount, 12),
        "expected_exit_fee_at_stop": decimal_string(stop_fee, 12),
        "expected_exit_fee_at_target": decimal_string(target_fee, 12),
        "net_expected_reward_risk": decimal_string(net_rr, 8),
        "approved_risk_distance": decimal_string(approved_distance, 8),
    }


def exit_values(settings: Settings, net_quantity: Decimal, trigger_price: Decimal) -> dict[str, Decimal]:
    if min(net_quantity, trigger_price) <= 0:
        raise PolicyError("paper exit quantity and price must be positive")
    slip = settings.paper.slippage_pct / Decimal("100")
    fee_rate = settings.risk.paper_fee_pct / Decimal("100")
    exit_price = trigger_price * (Decimal("1") - slip)
    gross = net_quantity * exit_price
    fee = gross * fee_rate
    return {"exit_price": exit_price, "gross_proceeds": gross, "exit_fee": fee,
            "net_proceeds": gross - fee}
