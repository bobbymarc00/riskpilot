from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from .config import Settings
from .indicators import IndicatorSnapshot, analyze
from .market import Kline


SCORE_ENGINE_VERSION = "scheduled-signal-v1"


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    weight: int
    value: Any
    matched: bool
    contribution: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MarketScore:
    symbol: str
    snapshot: IndicatorSnapshot
    components: tuple[ScoreComponent, ...]
    native_total_score: int
    minimum_signal_score: int
    threshold_passed: bool
    market_signal_classification: str
    candidate_eligible: bool
    candidate_ineligibility_reason: str | None
    score_engine_version: str = SCORE_ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score_engine_version": self.score_engine_version,
            "native_total_score": self.native_total_score,
            "components": [item.to_dict() for item in self.components],
            "threshold_result": {
                "minimum_signal_score": self.minimum_signal_score,
                "passed": self.threshold_passed,
            },
            "market_signal_classification": self.market_signal_classification,
            "candidate_eligible": self.candidate_eligible,
            "candidate_ineligibility_reason": self.candidate_ineligibility_reason,
            "metrics": self.snapshot.to_dict(),
        }


def score_components(snapshot: IndicatorSnapshot) -> tuple[ScoreComponent, ...]:
    """Return the named contributions used by the original scheduled scorer."""
    rsi_weight = 20 if 50.0 <= snapshot.rsi_14 <= 68.0 else 10 if 45.0 <= snapshot.rsi_14 <= 72.0 else -10 if snapshot.rsi_14 > 78.0 else 0
    momentum_weight = 15 if 0.05 <= snapshot.momentum_3_pct <= 3.0 else 7 if snapshot.momentum_3_pct > 0.0 else 0
    volume_weight = 15 if snapshot.volume_ratio_20 >= 1.2 else 8 if snapshot.volume_ratio_20 >= 0.8 else 0
    values = (
        ("ema_fast_above_ema_slow", 25, snapshot.ema_fast > snapshot.ema_slow, snapshot.ema_fast > snapshot.ema_slow, 25 if snapshot.ema_fast > snapshot.ema_slow else 0),
        ("close_above_ema_fast", 10, snapshot.close > snapshot.ema_fast, snapshot.close > snapshot.ema_fast, 10 if snapshot.close > snapshot.ema_fast else 0),
        ("rsi_14", rsi_weight, snapshot.rsi_14, rsi_weight != 0, rsi_weight),
        ("momentum_3_pct", momentum_weight, snapshot.momentum_3_pct, momentum_weight != 0, momentum_weight),
        ("volume_ratio_20", volume_weight, snapshot.volume_ratio_20, volume_weight != 0, volume_weight),
        ("atr_pct_in_range", 10, snapshot.atr_pct, 0.15 <= snapshot.atr_pct <= 3.0, 10 if 0.15 <= snapshot.atr_pct <= 3.0 else 0),
        ("breakout_20", 5, snapshot.breakout_20, snapshot.breakout_20, 5 if snapshot.breakout_20 else 0),
    )
    return tuple(ScoreComponent(*item) for item in values)


def native_score(snapshot: IndicatorSnapshot) -> int:
    return max(0, min(100, sum(item.contribution for item in score_components(snapshot))))


def candidate_eligibility(settings: Settings, symbol: str,
                          snapshot: IndicatorSnapshot, total: int | None = None) -> tuple[bool, str | None]:
    total = native_score(snapshot) if total is None else total
    threshold_passed = total >= settings.market.min_signal_score
    bullish = (
        symbol in settings.market.symbols
        and snapshot.ema_fast > snapshot.ema_slow
        and snapshot.close > snapshot.ema_fast
        and 45.0 <= snapshot.rsi_14 <= 72.0
        and snapshot.momentum_3_pct > 0.0
        and snapshot.atr_pct <= float(settings.risk.max_stop_distance_pct)
    )
    if symbol not in settings.market.symbols:
        return False, "symbol is not configured"
    if not threshold_passed:
        return False, f"native score {total} is below minimum {settings.market.min_signal_score}"
    if not bullish:
        return False, "existing scheduled bullish eligibility gate did not pass"
    return True, None


def score_market(settings: Settings, symbol: str, klines: Sequence[Kline]) -> MarketScore:
    """Pure canonical score and scheduled candidate decision for validated candles."""
    snapshot = analyze(klines)
    return score_snapshot(settings, symbol, snapshot, klines[-2].close)


def score_snapshot(settings: Settings, symbol: str, snapshot: IndicatorSnapshot,
                   previous_close: float) -> MarketScore:
    """Canonical result for the scheduler's established indicator snapshot seam."""
    components = score_components(snapshot)
    total = max(0, min(100, sum(item.contribution for item in components)))
    threshold_passed = total >= settings.market.min_signal_score
    eligible, reason = candidate_eligibility(settings, symbol, snapshot, total)
    direction = "UP" if snapshot.close > previous_close else "DOWN" if snapshot.close < previous_close else "FLAT"
    return MarketScore(
        symbol=symbol,
        snapshot=snapshot,
        components=components,
        native_total_score=total,
        minimum_signal_score=settings.market.min_signal_score,
        threshold_passed=threshold_passed,
        market_signal_classification=direction,
        candidate_eligible=eligible,
        candidate_ineligibility_reason=reason,
    )
