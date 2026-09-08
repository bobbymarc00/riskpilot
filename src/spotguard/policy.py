from __future__ import annotations

import secrets
from datetime import timedelta
from decimal import Decimal, ROUND_UP
from typing import Any

from .config import Settings
from .util import bounded_text, canonical_json, decimal_string, decimal_value, isoformat, parse_time, utcnow


class PolicyError(RuntimeError):
    pass


def _proposal_id() -> str:
    return f"p-{secrets.token_hex(6)}"


def _exact_decimal_string(value: Decimal) -> str:
    """Render a finite Decimal without discarding precision."""
    rendered = format(value, "f").rstrip("0").rstrip(".")
    return rendered or "0"


def _canonical_paper_bracket(entry_reference: Decimal, stop_reference: Decimal,
                             minimum_reward_risk: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return a PAPER bracket whose persisted price references remain self-consistent.

    Proposal prices are persisted to eight decimal places.  Derive the target from
    the persisted entry/stop distance rather than independently truncating a target
    derived from higher-precision references.  Rounding the target upward preserves
    the configured minimum reward/risk after serialization.
    """
    entry = Decimal(decimal_string(entry_reference, 8))
    stop = Decimal(decimal_string(stop_reference, 8))
    if stop <= 0 or stop >= entry:
        raise PolicyError("serialized PAPER stop reference is invalid")
    quantum = Decimal("0.00000001")
    target = (entry + (entry - stop) * minimum_reward_risk).quantize(
        quantum, rounding=ROUND_UP
    )
    reward_risk = (target - entry) / (entry - stop)
    if reward_risk < minimum_reward_risk:
        raise PolicyError("serialized PAPER reward/risk is below the configured minimum")
    return entry, stop, target, reward_risk


def entry_policy_terms(settings: Settings, *, candidate_price: Any,
                       atr_value: Any, bid_reference: Decimal,
                       ask_reference: Decimal, quote_amount: Decimal,
                       mode: str) -> dict[str, Decimal]:
    """Pure shared entry-policy math used by proposals and hypothetical checks."""
    if bid_reference <= 0 or ask_reference <= 0 or ask_reference < bid_reference:
        raise PolicyError("bid/ask references are invalid")
    midpoint = (bid_reference + ask_reference) / Decimal("2")
    spread_pct = (ask_reference - bid_reference) / midpoint * Decimal("100")
    if spread_pct > settings.market.max_spread_pct:
        raise PolicyError(
            f"Agent OS order-book spread is {decimal_string(spread_pct, 4)}%, above the configured limit"
        )
    entry_reference = (ask_reference * (Decimal("1") + settings.paper.slippage_pct / Decimal("100"))) if mode == "live" else ask_reference
    maximum_quote = settings.live.max_live_trade_usdt if mode == "live" else settings.paper.max_quote_per_entry_usdt
    if not settings.risk.min_quote_amount <= quote_amount <= maximum_quote:
        raise PolicyError(
            f"quote amount must be between {settings.risk.min_quote_amount} and {maximum_quote}"
        )
    candidate_price = decimal_value(candidate_price, "candidate.price")
    drift_pct = abs((entry_reference - candidate_price) / candidate_price) * Decimal("100")
    if drift_pct > settings.market.max_entry_drift_pct:
        raise PolicyError(
            f"fresh Agent OS price drifted {decimal_string(drift_pct, 3)}%, above the configured limit"
        )
    atr_value = decimal_value(atr_value, "candidate.metrics.atr_14")
    minimum_distance = entry_reference * settings.risk.min_stop_distance_pct / Decimal("100")
    stop_distance = max(atr_value * settings.risk.atr_stop_multiplier, minimum_distance)
    stop_distance_pct = stop_distance / entry_reference * Decimal("100")
    if stop_distance_pct > settings.risk.max_stop_distance_pct:
        raise PolicyError(
            f"ATR-based stop distance is {decimal_string(stop_distance_pct, 3)}%, above the risk limit"
        )
    stop_reference = entry_reference - stop_distance
    if stop_reference <= 0:
        raise PolicyError("computed stop reference is invalid")
    take_profit_reference = entry_reference + (stop_distance * settings.risk.min_reward_risk)
    reward_risk = (take_profit_reference - entry_reference) / (entry_reference - stop_reference)
    if reward_risk < settings.risk.min_reward_risk:
        raise PolicyError("reward/risk ratio is below the configured minimum")
    return {"spread_pct": spread_pct, "entry_reference": entry_reference,
            "stop_reference": stop_reference, "take_profit_reference": take_profit_reference,
            "reward_risk": reward_risk}


def build_proposal(
    settings: Settings,
    candidate: dict[str, Any],
    bid_reference: Decimal,
    ask_reference: Decimal,
    quote_amount: Decimal,
    rationale: str,
    proposal_mode: str | None = None,
    source: str = "deterministic-signal",
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    if candidate["status"] != "ACTIVE":
        raise PolicyError(f"candidate must be ACTIVE, not {candidate['status']}")
    if parse_time(candidate["expires_at"]) <= utcnow():
        raise PolicyError("candidate has expired")
    if candidate["side"] != "BUY":
        raise PolicyError("RiskPilot only creates BUY entry proposals")
    if candidate["symbol"] not in settings.market.symbols:
        raise PolicyError("candidate symbol is no longer allowlisted")
    mode = proposal_mode or settings.mode
    order_type = "LIMIT" if mode == "live" else "MARKET"
    terms = entry_policy_terms(settings, candidate_price=candidate["price"],
                               atr_value=candidate["metrics"]["atr_14"],
                               bid_reference=bid_reference, ask_reference=ask_reference,
                               quote_amount=quote_amount, mode=mode)
    spread_pct = terms["spread_pct"]
    entry_reference = terms["entry_reference"]
    stop_reference = terms["stop_reference"]
    take_profit_reference = terms["take_profit_reference"]
    reward_risk = terms["reward_risk"]
    if mode == "paper":
        entry_reference, stop_reference, take_profit_reference, reward_risk = _canonical_paper_bracket(
            entry_reference, stop_reference, settings.risk.min_reward_risk
        )

    now = utcnow()
    proposal_id = _proposal_id()
    normalized_rationale = bounded_text(rationale, "rationale", maximum=500)
    canonical = {
        "schema": "spotguard.order.v1",
        "proposal_id": proposal_id,
        "candidate_id": candidate["id"],
        "product": "SPOT",
        "symbol": candidate["symbol"],
        "side": "BUY",
        "order_type": order_type,
        "quote_asset": settings.risk.quote_asset,
        "quote_amount": decimal_string(quote_amount, 8),
        "bid_reference": decimal_string(bid_reference, 8),
        "ask_reference": decimal_string(ask_reference, 8),
        "spread_pct": decimal_string(spread_pct, 6),
        "entry_reference": decimal_string(entry_reference, 8),
        "current_ask": decimal_string(ask_reference, 8),
        "entry_limit_price": decimal_string(entry_reference, 8) if mode == "live" else None,
        "stop_reference": decimal_string(stop_reference, 8),
        "take_profit_reference": decimal_string(take_profit_reference, 8),
        # PAPER may need more than four decimal places to faithfully represent
        # the ratio implied by its persisted eight-decimal price references.
        "reward_risk": (_exact_decimal_string(reward_risk) if mode == "paper"
                        else decimal_string(reward_risk, 4)),
        "mode": mode,
        "source": source,
        "approval_owner_id": settings.openclaw.telegram_owner_id,
        "approval_chat_id": settings.telegram.chat_id,
        "approval_nonce": secrets.token_urlsafe(12),
        "created_at": isoformat(now),
        "expires_at": isoformat(now + (timedelta(seconds=ttl_seconds) if ttl_seconds else timedelta(minutes=settings.risk.proposal_ttl_minutes))),
    }
    return {
        "id": proposal_id,
        "candidate_id": candidate["id"],
        "symbol": candidate["symbol"],
        "side": "BUY",
        "product": "SPOT",
        "order_type": order_type,
        "quote_amount": canonical["quote_amount"],
        "entry_reference": canonical["entry_reference"],
        "stop_reference": canonical["stop_reference"],
        "take_profit_reference": canonical["take_profit_reference"],
        "reward_risk": canonical["reward_risk"],
        "rationale": normalized_rationale,
        "mode": mode,
        "source": source,
        "created_at": canonical["created_at"],
        "expires_at": canonical["expires_at"],
        "canonical": canonical,
        "canonical_json": canonical_json(canonical),
    }


def validate_claim(settings: Settings, proposal: dict[str, Any], daily_committed_quote: Decimal) -> None:
    canonical = proposal["canonical"]
    if proposal["status"] != "PENDING":
        raise PolicyError(f"proposal must be PENDING, not {proposal['status']}")
    if parse_time(proposal["expires_at"]) <= utcnow():
        raise PolicyError("proposal has expired")
    if canonical.get("source") == "manual-live-set-protection":
        expected={"product":"SPOT","side":"SELL","order_type":"OCO_PROTECTION","mode":"live","quote_asset":settings.risk.quote_asset}
        if any(canonical.get(k)!=v for k,v in expected.items()): raise PolicyError("live protection restore terms do not match current policy")
        quantity=decimal_value(canonical.get("quantity"),"protection.quantity"); step=decimal_value(canonical.get("market_step_size"),"protection.market_step_size")
        stop=decimal_value(canonical.get("stop_reference"),"protection.stop_reference"); target=decimal_value(canonical.get("take_profit_reference"),"protection.take_profit_reference")
        if quantity<=0 or step<=0 or quantity%step or not stop<target: raise PolicyError("live protection restore quantities or bracket are invalid")
        return
    if canonical.get("source") == "manual-live-partial-exit":
        expected={"product":"SPOT","side":"SELL","order_type":"PARTIAL_EXIT","mode":"live","quote_asset":settings.risk.quote_asset}
        if any(canonical.get(k)!=v for k,v in expected.items()): raise PolicyError("live partial exit terms do not match current policy")
        percentage=decimal_value(canonical.get("percentage"),"partial_exit.percentage")
        sell=decimal_value(canonical.get("sell_quantity"),"partial_exit.sell_quantity"); remaining=decimal_value(canonical.get("remaining_quantity"),"partial_exit.remaining_quantity"); step=decimal_value(canonical.get("market_step_size"),"partial_exit.market_step_size")
        if not Decimal("0") < percentage <= Decimal("100") or sell<=0 or remaining<0 or step<=0 or sell%step or remaining%step: raise PolicyError("live partial exit quantities are invalid")
        return
    if canonical.get("source") == "manual-live-cancel-protection":
        expected_cancel = {"product": "SPOT", "side": "CANCEL", "order_type": "CANCEL_OCO", "mode": "live",
                           "quote_asset": settings.risk.quote_asset}
        for key, value in expected_cancel.items():
            if canonical.get(key) != value:
                raise PolicyError(f"live cancel {key} does not match current policy")
        if canonical.get("symbol") not in settings.market.symbols or not isinstance(canonical.get("order_list_id"), int) or not isinstance(canonical.get("cancel_order_id"), int):
            raise PolicyError("live cancel protection identifiers are invalid")
        return
    if canonical.get("source") == "manual-live-close":
        expected_close = {"product": "SPOT", "side": "SELL", "order_type": "MARKET", "mode": "live",
                          "quote_asset": settings.risk.quote_asset}
        for key, value in expected_close.items():
            if canonical.get(key) != value:
                raise PolicyError(f"live close {key} does not match current policy")
        if canonical.get("symbol") not in settings.market.symbols:
            raise PolicyError("live close symbol is no longer allowlisted")
        quantity = decimal_value(canonical.get("quantity"), "live_close.quantity")
        step = decimal_value(canonical.get("market_step_size"), "live_close.market_step_size")
        if quantity <= 0 or step <= 0 or quantity % step != 0:
            raise PolicyError("live close quantity is invalid or not exchange-aligned")
        return
    expected = {
        "product": "SPOT",
        "side": "BUY",
        "order_type": "LIMIT" if proposal["mode"] == "live" else "MARKET",
        "mode": proposal["mode"],
        "quote_asset": settings.risk.quote_asset,
    }
    for key, value in expected.items():
        if canonical.get(key) != value:
            raise PolicyError(f"proposal {key} does not match current policy")
    if canonical.get("symbol") not in settings.market.symbols:
        raise PolicyError("proposal symbol is no longer allowlisted")
    quote = decimal_value(canonical.get("quote_amount"), "proposal.quote_amount")
    maximum = settings.live.max_live_trade_usdt if proposal["mode"] == "live" else settings.paper.max_quote_per_entry_usdt
    if not settings.risk.min_quote_amount <= quote <= maximum:
        raise PolicyError("proposal quote amount violates the current per-trade limit")
    if proposal["mode"] == "live" and daily_committed_quote + quote > settings.risk.max_daily_quote:
        raise PolicyError("proposal would exceed the current daily quote limit")
    reward_risk = decimal_value(canonical.get("reward_risk"), "proposal.reward_risk")
    if reward_risk < settings.risk.min_reward_risk:
        raise PolicyError("proposal reward/risk is below the current minimum")
    spread_pct = decimal_value(canonical.get("spread_pct"), "proposal.spread_pct")
    if spread_pct > settings.market.max_spread_pct:
        raise PolicyError("proposal spread violates the current market limit")
    stop = decimal_value(canonical.get("stop_reference"), "proposal.stop_reference")
    entry = decimal_value(canonical.get("entry_reference"), "proposal.entry_reference")
    target = decimal_value(canonical.get("take_profit_reference"), "proposal.take_profit_reference")
    if not stop < entry < target:
        raise PolicyError("proposal bracket must satisfy stop < entry < take-profit")
    actual_reward_risk = (target - entry) / (entry - stop)
    if actual_reward_risk <= 0 or abs(actual_reward_risk - reward_risk) > Decimal("0.0001"):
        raise PolicyError("proposal bracket reward/risk does not match the canonical ratio")
    distance_pct = (entry - stop) / entry * Decimal("100")
    if distance_pct < settings.risk.min_stop_distance_pct or distance_pct > settings.risk.max_stop_distance_pct:
        raise PolicyError("proposal stop distance violates the current risk limits")


def execution_intent(settings: Settings, proposal: dict[str, Any]) -> dict[str, Any]:
    canonical = proposal["canonical"]
    if canonical.get("source") == "manual-live-cancel-protection":
        return {"server": settings.codex.mcp_server, "product": "SPOT", "operation": "cancel exact protected Spot OCO list",
                "arguments": {"symbol": canonical["symbol"], "order_list_id": canonical["order_list_id"], "order_id": canonical["cancel_order_id"]},
                "constraints": {"single_call": True, "withdrawal": False, "transfer": False, "futures": False, "margin": False, "retry_on_unknown_result": False}}
    if canonical.get("source") == "manual-live-close":
        return {
            "server": settings.codex.mcp_server, "product": "SPOT",
            "operation": "submit exact Spot MARKET SELL close",
            "arguments": {"symbol": canonical["symbol"], "side": "SELL", "order_type": "MARKET",
                          "quantity": canonical["quantity"]},
            "constraints": {"single_call": True, "withdrawal": False, "transfer": False, "futures": False,
                            "margin": False, "retry_on_unknown_result": False},
        }
    return {
        "server": settings.codex.mcp_server,
        "product": "SPOT",
        "operation": "submit exact protected Spot order list" if proposal["mode"] == "live" else "simulate one spot market buy",
        "arguments": {
            "symbol": canonical["symbol"],
            "side": "BUY",
            "order_type": canonical["order_type"],
            "quote_amount": canonical["quote_amount"],
            "quote_asset": canonical["quote_asset"],
        },
        "constraints": {
            "single_call": True,
            "do_not_increase_quote_amount": True,
            "withdrawal": False,
            "transfer": False,
            "futures": False,
            "margin": False,
            "retry_on_unknown_result": False,
        },
        "references_not_automatic_orders": {
            "stop": canonical["stop_reference"],
            "take_profit": canonical["take_profit_reference"],
        },
    }
