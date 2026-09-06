from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any
from .presentation import catalog, detect_locale, translate, vocabulary

BUY_WORDS = vocabulary("input.buy")
CLOSE_WORDS = vocabulary("input.close")
FILLER = vocabulary("input.filler")
CONDITIONAL = vocabulary("input.conditional")
QUESTION = vocabulary("input.question")


def normalize_paper_intent(text: str, allowed_symbols: tuple[str, ...], locale: str | None = None) -> dict[str, Any]:
    locale = locale or detect_locale(text)
    t = lambda key, **values: translate(key, locale, **values)
    raw = text.strip()
    lowered = re.sub(r"\b" + catalog("id")["input.typo"] + r"\b", catalog("id")["input.typo_target"], raw.lower())
    if not raw or raw.startswith(">") or "forwarded" in lowered or "“" in raw or '"' in raw:
        return {"action": "clarify", "message": t("intent.direct")}
    words = re.findall(r"-?\d+(?:\.\d+)?|[a-zA-Z]+|[%?]", lowered)
    if lowered.rstrip(" ?.!") in vocabulary("input.ranking"):
        return {"action": "ranking"}
    if words and words[0] in vocabulary("input.analysis"):
        symbols = [word.upper() for word in words[1:]
                   if word.isalpha() and word not in FILLER and word not in {"usd", "usdt"}]
        amounts = [word for word in words[1:] if re.fullmatch(r"-?\d+(?:\.\d+)?", word)]
        if len(symbols) == 1 and len(amounts) <= 1:
            result = {"action": "analysis", "symbol": symbols[0]}
            if amounts:
                try:
                    amount = Decimal(amounts[0])
                except InvalidOperation:
                    return {"action": "clarify", "message": t("intent.buy_fields")}
                if amount <= 0:
                    return {"action": "invalid", "message": t("intent.buy_positive")}
                result["quote_amount"] = str(amount)
            return result
        return {"action": "clarify", "message": t("intent.close_symbol")}
    if "?" in words or any(word in QUESTION for word in words):
        return {"action": "info", "message": t("intent.question")}
    if any(word in CONDITIONAL for word in words):
        return {"action": "unsupported", "message": t("intent.conditional")}
    if lowered in vocabulary("input.positions"):
        return {"action": "positions"}
    if lowered in vocabulary("input.balance"):
        return {"action": "balance"}
    if lowered in vocabulary("input.status"):
        return {"action": "status"}
    if any(word in vocabulary("input.reject") for word in words) and "pending" in words:
        return {"action":"reject_pending","buy_deferred":any(word in BUY_WORDS for word in words),
                "message":t("intent.rejected_pending")}
    symbols = []
    aliases = {symbol[:-4]: symbol for symbol in allowed_symbols if symbol.endswith("USDT")}
    aliases.update({symbol: symbol for symbol in allowed_symbols})
    for word in words:
        upper = word.upper()
        if upper in aliases and aliases[upper] not in symbols:
            symbols.append(aliases[upper])
    amounts = []
    for word in words:
        if re.fullmatch(r"-?\d+(?:\.\d+)?", word):
            try:
                amounts.append(Decimal(word))
            except InvalidOperation:
                return {"action": "invalid", "message": t("intent.amount")}
    has_buy = any(word in BUY_WORDS for word in words)
    has_close = any(word in CLOSE_WORDS for word in words)
    if (has_buy or has_close) and not symbols:
        known_words = BUY_WORDS | CLOSE_WORDS | FILLER | CONDITIONAL | QUESTION | vocabulary("input.all") | vocabulary("input.live") | {"%"}
        unknown = [word for word in words if word.isalpha() and word not in known_words]
        if unknown:
            return {"action": "invalid", "message": t("intent.symbol", symbol=unknown[-1].upper())}
    if has_buy and any(word in vocabulary("input.live") for word in words):
        return {"action": "explicit_live", "message": t("intent.live")}
    if has_close:
        close_symbol = None
        for index, word in enumerate(words[:-1]):
            if word in CLOSE_WORDS and words[index + 1].upper() in aliases:
                close_symbol = aliases[words[index + 1].upper()]
                break
        if close_symbol is None and len(symbols) == 1:
            close_symbol = symbols[0]
        if close_symbol is None:
            return {"action": "clarify", "message": t("intent.close_symbol")}
        if set(words) & vocabulary("input.all"):
            return {"action": "close", "symbol": close_symbol, "percentage": "100",
                    "close_selector": "all", "buy_deferred": has_buy,
                    "message": t("intent.deferred") if has_buy else None}
        percentage=Decimal("100")
        if "%" in words:
            idx=words.index("%")
            if idx==0 or not re.fullmatch(r"-?\d+(?:\.\d+)?",words[idx-1]):
                return {"action":"invalid","message":t("intent.percentage")}
            percentage=Decimal(words[idx-1])
            if percentage <= 0 or percentage > 100:
                return {"action":"invalid","message":t("intent.percentage_range")}
            return {"action": "close", "symbol": close_symbol, "percentage": str(percentage),
                    "close_selector": "percentage", "buy_deferred": has_buy,
                    "message": t("intent.deferred") if has_buy else None}
        unit_indices = [idx for idx, word in enumerate(words) if word in {"usd", "usdt"}]
        if unit_indices:
            idx = unit_indices[0]
            if idx == 0 or not re.fullmatch(r"-?\d+(?:\.\d+)?", words[idx - 1]):
                return {"action": "invalid", "message": t("intent.close_quote")}
            quote = Decimal(words[idx - 1])
            if quote <= 0:
                return {"action": "invalid", "message": t("intent.close_positive")}
            return {"action": "close", "symbol": close_symbol, "percentage": "100",
                    "close_selector": "quote", "close_quote_amount": str(quote),
                    "buy_deferred": has_buy,
                    "message": t("intent.deferred") if has_buy else None}
        if len(amounts) != 1:
            return {"action": "invalid", "message": t("intent.selector")}
        if amounts[0] <= 0:
            return {"action": "invalid", "message": t("intent.close_positive")}
        return {"action": "close", "symbol": close_symbol, "percentage": "100",
                "close_selector": "quantity", "close_quantity": str(amounts[0]),
                "buy_deferred": has_buy,
                "message": t("intent.deferred") if has_buy else None}
    if has_buy:
        if len(symbols) != 1 or len(set(amounts)) != 1 or not amounts:
            return {"action": "clarify", "message": t("intent.buy_fields")}
        if amounts[0] <= 0:
            return {"action": "invalid", "message": t("intent.buy_positive")}
        return {"action": "buy", "symbol": symbols[0], "quote_amount": str(amounts[0]), "mode": "paper"}
    if lowered in vocabulary("input.approval"):
        return {"action": "forbidden_approval", "message": t("intent.approval")}
    return {"action": "clarify", "message": t("intent.clarify")}


def normalize_trade_intent(text: str, allowed_symbols: tuple[str, ...], locale: str | None = None) -> dict[str, Any]:
    """Normalize the protected default route: LIVE unless `paper` is explicit.

    The legacy paper normalizer remains unchanged for demos and existing button
    flows.  Removing an explicit LIVE marker before reusing its strict parser
    prevents that marker from becoming an unstructured execution instruction.
    """
    explicit_paper = bool(re.search(r"\bpaper\b", text, re.I))
    stripped = re.sub(r"\b(?:live|real|nyata)\b", "", text, flags=re.I)
    result = normalize_paper_intent(stripped, allowed_symbols, locale)
    if result.get("action") == "buy":
        result["mode"] = "paper" if explicit_paper else "live"
    return result
