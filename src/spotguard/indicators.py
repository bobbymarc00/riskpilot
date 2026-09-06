from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any, Sequence

from .market import Kline


class IndicatorError(ValueError):
    pass


def ema(values: Sequence[float], period: int) -> list[float]:
    if period < 2 or len(values) < period:
        raise IndicatorError(f"EMA({period}) requires at least {period} values")
    multiplier = 2.0 / (period + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append((float(value) - result[-1]) * multiplier + result[-1])
    return result


def rsi(values: Sequence[float], period: int = 14) -> float:
    if len(values) <= period:
        raise IndicatorError(f"RSI({period}) requires more than {period} values")
    deltas = [float(current) - float(previous) for previous, current in zip(values, values[1:])]
    gains = [max(delta, 0.0) for delta in deltas]
    losses = [max(-delta, 0.0) for delta in deltas]
    average_gain = fmean(gains[:period])
    average_loss = fmean(losses[:period])
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = ((average_gain * (period - 1)) + gain) / period
        average_loss = ((average_loss * (period - 1)) + loss) / period
    if average_loss == 0:
        return 100.0
    strength = average_gain / average_loss
    return 100.0 - (100.0 / (1.0 + strength))


def atr(klines: Sequence[Kline], period: int = 14) -> float:
    if len(klines) <= period:
        raise IndicatorError(f"ATR({period}) requires more than {period} klines")
    ranges: list[float] = []
    for previous, current in zip(klines, klines[1:]):
        ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    current_atr = fmean(ranges[:period])
    for true_range in ranges[period:]:
        current_atr = ((current_atr * (period - 1)) + true_range) / period
    return current_atr


@dataclass(frozen=True)
class IndicatorSnapshot:
    close: float
    ema_fast: float
    ema_slow: float
    rsi_14: float
    atr_14: float
    atr_pct: float
    momentum_3_pct: float
    volume_ratio_20: float
    breakout_20: bool
    candle_close_time: int
    score: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def analyze(klines: Sequence[Kline]) -> IndicatorSnapshot:
    if len(klines) < 60:
        raise IndicatorError("at least 60 klines are required")
    closes = [item.close for item in klines]
    volumes = [item.volume for item in klines]
    fast = ema(closes, 12)[-1]
    slow = ema(closes, 26)[-1]
    current_rsi = rsi(closes, 14)
    current_atr = atr(klines, 14)
    close = closes[-1]
    atr_pct = (current_atr / close) * 100 if close else 0.0
    momentum = ((close / closes[-4]) - 1.0) * 100 if closes[-4] else 0.0
    baseline_volume = fmean(volumes[-21:-1])
    volume_ratio = volumes[-1] / baseline_volume if baseline_volume else 0.0
    breakout = close >= max(closes[-21:-1])

    snapshot = IndicatorSnapshot(
        close=close,
        ema_fast=fast,
        ema_slow=slow,
        rsi_14=current_rsi,
        atr_14=current_atr,
        atr_pct=atr_pct,
        momentum_3_pct=momentum,
        volume_ratio_20=volume_ratio,
        breakout_20=breakout,
        candle_close_time=klines[-1].close_time,
        score=0,
    )
    # Imported lazily to keep the long-standing indicators API free of an
    # import cycle while making this calculation canonical for every caller.
    from .score_engine import native_score
    return IndicatorSnapshot(**{**snapshot.to_dict(), "score": native_score(snapshot)})
