from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any

from .config import Settings
from .indicators import IndicatorSnapshot
from .score_engine import MarketScore, SCORE_ENGINE_VERSION, candidate_eligibility


@dataclass(frozen=True)
class Signal:
    candidate_id: str
    fingerprint: str
    symbol: str
    interval: str
    side: str
    score: int
    price: float
    candle_close_time: int
    reasons: tuple[str, ...]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["reasons"] = list(self.reasons)
        return result


def evaluate(settings: Settings, symbol: str, snapshot: IndicatorSnapshot) -> Signal | None:
    eligible, _ = candidate_eligibility(settings, symbol, snapshot, snapshot.score)
    if not eligible:
        return None

    reasons: list[str] = ["EMA12 is above EMA26", "price is above EMA12"]
    if 50.0 <= snapshot.rsi_14 <= 68.0:
        reasons.append("RSI is positive without being extremely extended")
    if snapshot.volume_ratio_20 >= 1.2:
        reasons.append("volume is above its 20-candle baseline")
    if snapshot.breakout_20:
        reasons.append("price is testing a 20-candle high")

    fingerprint = hashlib.sha256(
        f"{symbol}:{settings.market.interval}:{snapshot.candle_close_time}:BUY".encode("utf-8")
    ).hexdigest()
    candidate_id = f"c-{fingerprint[:12]}"
    return Signal(
        candidate_id=candidate_id,
        fingerprint=fingerprint,
        symbol=symbol,
        interval=settings.market.interval,
        side="BUY",
        score=snapshot.score,
        price=snapshot.close,
        candle_close_time=snapshot.candle_close_time,
        reasons=tuple(reasons),
        metrics={**snapshot.to_dict(), "score_engine_version": SCORE_ENGINE_VERSION},
    )


def signal_from_market_score(settings: Settings, result: MarketScore) -> Signal | None:
    """Build the unchanged scheduled Signal identity from a canonical result."""
    if not result.candidate_eligible:
        return None
    return evaluate(settings, result.symbol, result.snapshot)
