"""Deterministic presentation. Financial and authorization payloads stay intact."""
from __future__ import annotations

import json
import re
from decimal import Decimal
from functools import lru_cache, wraps
from inspect import signature
from pathlib import Path
from typing import Any


ANALYSIS_STRENGTH_KEYS = {
    "very_weak": "analysis.strength.very_weak", "weak": "analysis.strength.weak",
    "moderate": "analysis.strength.moderate", "strong": "analysis.strength.strong",
}
ANALYSIS_CONCLUSION_KEYS = {
    "UP": "analysis.conclusion.up", "DOWN": "analysis.conclusion.down", "FLAT": "analysis.conclusion.flat",
}
ANALYSIS_SCENARIO_KEYS = {
    "UP": "analysis.scenario.up", "DOWN": "analysis.scenario.down", "FLAT": "analysis.scenario.flat",
}


@lru_cache(maxsize=2)
def catalog(locale: str) -> dict[str, str]:
    if locale not in {"en", "id"}:
        raise ValueError("unsupported presentation locale")
    return json.loads((Path(__file__).parent / "locales" / f"{locale}.json").read_text(encoding="utf-8"))


def translate(key: str, locale: str = "en", **values: Any) -> str:
    return catalog(locale)[key].format(**values)


def vocabulary(key: str) -> set[str]:
    return set((catalog("en")[key] + "|" + catalog("id")[key]).split("|"))


def detect_locale(text: str, previous: str | None = None, default: str = "id") -> str:
    words = set(re.findall(r"[a-z]+", text.lower()))
    if words & set(catalog("id")["input.locale_words"].split("|")):
        return "id"
    if words & set(catalog("en")["input.locale_words"].split("|")):
        return "en"
    return previous if previous in {"en", "id"} else default


def number(value: Any, locale: str, places: int = 2) -> str:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("non-finite presentation number")
    result = format(amount, f",.{places}f")
    return result.translate(str.maketrans({",": ".", ".": ","})) if locale == "id" else result


def compact_number(value: Any, locale: str, maximum_places: int = 4, minimum_places: int = 0) -> str:
    """Format a display value without changing its stored Decimal representation."""
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("non-finite presentation number")
    text = format(amount, f",.{maximum_places}f")
    whole, fraction = text.split(".")
    fraction = fraction.rstrip("0")
    if len(fraction) < minimum_places:
        fraction += "0" * (minimum_places - len(fraction))
    result = whole + ("." + fraction if fraction else "")
    return result.translate(str.maketrans({",": ".", ".": ","})) if locale == "id" else result


def error_text(error: Exception, locale: str) -> str:
    detail = str(error).lower()
    maximum = re.search(r"requested amount ([0-9.]+) usdt exceeds configured maximum ([0-9.]+) usdt", detail)
    if maximum:
        return translate("policy.max_order_exceeded", locale, amount=compact_number(maximum.group(1), locale, 4), maximum=compact_number(maximum.group(2), locale, 4))
    if "quote amount must be positive" in detail:
        return translate("policy.non_positive", locale)
    key = "error.unavailable"
    if "expired" in detail:
        key = "approval.expired"
    elif any(term in detail for term in ("not pending", "not claimable", "not executing", "replay", "already", "rejected from")):
        key = "approval.replay"
    elif "minimum" in detail or "below" in detail or "notional" in detail:
        key = "policy.min_order"
    elif "maximum" in detail or "no more than" in detail:
        key = "policy.risk_limit"
    elif any(term in detail for term in ("risk", "limit", "quota", "exposure", "insufficient", "dust", "cap")):
        key = "policy.risk_limit"
    elif type(error).__name__ == "SecurityError":
        key = "error.security"
    return translate(key, locale)


def momentum_strength(change_pct: Any) -> str:
    """Classify absolute close-to-close momentum for display only."""
    magnitude = abs(Decimal(str(change_pct)))
    if magnitude < Decimal("0.10"):
        return "very_weak"
    if magnitude < Decimal("0.30"):
        return "weak"
    if magnitude < Decimal("0.75"):
        return "moderate"
    return "strong"


def analysis_reason(reason: Any, locale: str) -> str:
    if not reason:
        return translate("analysis.none", locale)
    text = str(reason)
    if locale == "en":
        return text
    patterns = (
        (r"native score (\d+) is below minimum (\d+)", "analysis.reason.score", ("score", "minimum")),
        (r"active PAPER tranche limit reached \((\d+)\)", "analysis.reason.tranches", ("limit",)),
        (r"economic PAPER position/distinct-symbol limit reached \((\d+)\)", "analysis.reason.positions", ("limit",)),
        (r"daily PAPER entry quota reached \((\d+)(?: successful BUY fills per UTC day)?\)", "analysis.reason.quota", ("limit",)),
        (r"a pending or processing paper BUY already exists for ([A-Z0-9]+)", "analysis.reason.pending_symbol", ("symbol",)),
        (r"PAPER exposure limit reached: projected exposure would exceed ([0-9.]+) USDT", "analysis.reason.exposure", ("limit",)),
    )
    for pattern, key, names in patterns:
        match = re.fullmatch(pattern, text)
        if match:
            return translate(key, locale, **dict(zip(names, match.groups())))
    exact = {
        "existing scheduled bullish eligibility gate did not pass": "analysis.reason.bullish_gate",
        "maximum active proposal count has been reached": "analysis.reason.pending_limit",
        "paper aggregate open risk limit is exhausted": "analysis.reason.aggregate_risk",
        "paper daily realized loss cap is exhausted": "analysis.reason.daily_loss",
        "insufficient free paper USDT": "analysis.reason.balance",
        "Agent OS confirmation did not match": "analysis.reason.confirmation",
    }
    if text in exact:
        return translate(exact[text], locale)
    # Preserve a fully localized response for an uncommon fail-closed policy
    # reason while the structured output retains the exact technical reason.
    return error_text(RuntimeError(text), locale)


def _analysis_fields(result: dict[str, Any], locale: str) -> dict[str, str]:
    candle = result["candle"]
    indicators = result["indicators"]
    n = lambda value: compact_number(value, locale, 8)
    pct = lambda value: compact_number(value, locale, 4)
    strength = momentum_strength(indicators["closed_candle_change_pct"])
    return {
        "symbol": result["symbol"], "interval": result["interval"],
        "timestamp": result["latest_closed_at"], "price": n(candle["close"]),
        "open_close": n(indicators["open_to_close_change"]),
        "open_close_pct": pct(indicators["open_to_close_change_pct"]),
        "close_close": n(indicators["closed_candle_change"]),
        "close_close_pct": pct(indicators["closed_candle_change_pct"]),
        "range": n(indicators["high_low_range"]), "support": n(indicators["local_support"]),
        "resistance": n(indicators["local_resistance"]), "volume": n(indicators["latest_volume"]),
        "signal": result["signal"], "strength": translate(ANALYSIS_STRENGTH_KEYS[strength], locale),
        "freshness": str(result["freshness_seconds"]),
        "score": str(result.get("native_signal_score", result.get("score", ""))),
        "version": str(result.get("score_engine_version", "")),
        "threshold": str(result.get("threshold_result", {}).get("minimum_signal_score", "")),
        "threshold_passed": translate("analysis.yes" if result.get("threshold_result", {}).get("passed") else "analysis.no", locale),
        "candidate_eligible": translate("analysis.yes" if result.get("candidate_eligible") else "analysis.no", locale),
        "candidate_reason": analysis_reason(result.get("candidate_ineligibility_reason"), locale),
        "confirmation": translate("analysis.confirmed" if result.get("agent_os_confirmation", {}).get("matched") else "analysis.not_confirmed", locale),
        "amount": n(result.get("hypothetical_order_amount_usdt", 0)),
        "amount_source": translate("analysis.amount.explicit" if result.get("hypothetical_order_amount_source") == "explicit" else "analysis.amount.default", locale),
        "execution_eligible": translate("analysis.yes" if result.get("execution_eligibility", {}).get("eligible") else "analysis.no", locale),
        "blocking_reason": analysis_reason(result.get("execution_eligibility", {}).get("blocking_reason"), locale),
        "position": translate("analysis.position.open" if result.get("paper_position_open") else "analysis.position.none", locale),
    }


def _score_components(result: dict[str, Any], locale: str) -> str:
    return "\n".join(translate("analysis.component.row", locale,
        name=item["name"], value=compact_number(item["value"], locale, 8) if not isinstance(item["value"], bool) else str(item["value"]).upper(),
        weight=item["weight"], contribution=item["contribution"])
        for item in result.get("score_components", []))


def render(result: dict[str, Any], locale: str, operation: str = "") -> str:
    t = lambda key, **fields: translate(key, locale, **fields)
    n = lambda value: compact_number(value, locale, 4)
    if result.get("score_engine_version") and "candle" in result:
        fields = _analysis_fields(result, locale)
        fields["components"] = _score_components(result, locale)
        return t("analysis.score.summary", **fields) + "\n" + t(ANALYSIS_CONCLUSION_KEYS[result["signal"]], **fields)
    if result.get("source") == "binance_agent_os_mcp" and "candle" in result:
        fields = _analysis_fields(result, locale)
        return t("analysis.summary", **fields) + "\n" + t(ANALYSIS_CONCLUSION_KEYS[result["signal"]], **fields)
    if "ranking" in result:
        rows = []
        for rank, row in enumerate(result["ranking"], start=1):
            fields = _analysis_fields(row, locale)
            fields["components"] = _score_components(row, locale)
            rows.append(t("analysis.score.ranking.row", rank=rank, **fields))
        eligible = result.get("execution_eligible_ranking", [])
        eligible_symbols = ", ".join(row["symbol"] for row in eligible) or t("analysis.none")
        eligible_candidates = result.get("eligible_candidates", [])
        selected = eligible_candidates[0] if eligible_candidates else result["ranking"][0]
        scenario = _analysis_fields(selected, locale)
        winner_reason = analysis_reason(result.get("highest_market_score_ineligibility_reason"), locale)
        return (t("analysis.score.ranking", eligible=eligible_symbols,
                  strongest=result.get("highest_scoring_eligible_candidate") or t("analysis.none"),
                  winner=result.get("highest_market_score_symbol") or t("analysis.none"),
                  winner_reason=winner_reason) + "\n" + "\n\n".join(rows) + "\n\n" +
                t(ANALYSIS_SCENARIO_KEYS[selected["signal"]], **scenario))
    if "close_proposal" in result:
        return t("proposal.close.created", identifier=result["close_proposal"]["id"])
    summary = result.get("execution_summary") or result.get("proposal", {}).get("execution_summary")
    if isinstance(summary, dict) and summary.get("simulated"):
        return t("approval.paper.filled", symbol=summary["symbol"],
                 quantity=compact_number(summary["net_base_quantity"], locale, 8),
                 price=compact_number(summary["average_fill_price"], locale, 8),
                 spend=compact_number(summary["actual_paper_spend"], locale, 4),
                 fee=compact_number(summary["simulated_fee"], locale, 8),
                 fee_asset=summary["symbol"].removesuffix("USDT"),
                 stop=compact_number(summary["stop_loss"], locale, 8),
                 target=compact_number(summary["take_profit"], locale, 8),
                 order_id=summary["paper_order_id"], position_id=summary["position_id"])
    if "reject" in operation:
        return t("approval.paper.rejected")
    if "approve" in operation or operation == "execute_paper":
        if "close" in operation:
            close = result.get("close", {})
            key = "position.close.full" if Decimal(str(close.get("requested_percentage", "100"))) == 100 else "position.close.partial"
            return t(key)
        return t("approval.paper.success")
    if "proposal" in result:
        return t("proposal.paper_buy.created", identifier=result["proposal"].get("id", ""))
    if "positions" in result or "free_usdt" in result or "balance" in result:
        balance = result.get("balance", result)
        text = t("balance.summary", current=n(balance.get("current_ledger_balance_usdt", 0)), free=n(balance.get("free_usdt", 0)), locked=n(balance.get("locked_cost_basis_usdt", balance.get("locked_usdt", 0))), initial=n(balance.get("initial_reset_balance_usdt", 0)), pnl=n(balance.get("realized_pnl_usdt", balance.get("realized_pnl", 0))), fees=n(balance.get("paid_fees_usdt", 0)), positions=balance.get("open_positions", len(result.get("positions", []))), tranches=balance.get("active_tranches", result.get("active_tranche_count", 0)))
        rows = result.get("positions", [])
        if "positions" in result:
            text += "\n" + t("position.title")
            text += "\n" + ("\n\n".join(t("position.row", symbol=row["symbol"], quantity=compact_number(row["net_quantity"], locale, 8), entry=compact_number(row.get("weighted_average_entry", row.get("average_fill_price", 0)), locale, 8), exposure=n(row.get("quote_spent", 0)), tranches=row.get("active_tranche_count", 0), stop=compact_number(row.get("final_stop", 0), locale, 8), target=compact_number(row.get("final_target", 0), locale, 8), risk=n(row.get("risk_amount", 0))) for row in rows) or t("position.empty"))
        return text
    if "intent" in result and result["intent"].get("message"):
        return result["intent"]["message"]
    if "payload" in result and result["payload"].get("message"):
        return result["payload"]["message"]
    return t("result.complete")


def localized(method):
    """Attach a display-only view and carry callback locale through failures."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bound = signature(method).bind(self, *args, **kwargs).arguments
        entity = bound.get("proposal_id") or bound.get("close_id")
        if "by_position" in method.__name__:
            entity = self.ledger.close_locale_entity(bound.get("position_id"), self.signer.token_hash(bound.get("code", "")))
        selected = self.ledger.presentation_locale("proposal", entity) if entity else None
        locale = (selected or self.settings.default_locale) if entity else self.locale
        if method.__name__ == "create_proposal":
            locale = self.ledger.presentation_locale("chat", self.settings.telegram.chat_id) or locale
        previous = self.locale
        self.locale = locale
        try:
            result = method(self, *args, **kwargs)
            if isinstance(result, dict):
                result["presentation"] = {"locale": locale, "text": render(result, locale, method.__name__)}
            return result
        except Exception as exc:
            exc.presentation_locale = getattr(exc, "presentation_locale", locale)
            raise
        finally:
            self.locale = previous
    return wrapped
