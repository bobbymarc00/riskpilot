#!/usr/bin/env python3
"""Additive RiskPilot smart-universe radar.

This script deliberately does NOT modify RiskPilot's existing config, LIVE
allowlist, proposal flow, execution flow, or risk policy. It builds a temporary
market universe in-memory for read-only market scoring and hands configured
symbols back to the existing scanner only when appropriate.

Design goals:
- one bulk 24h ticker pulse every 5 minutes for whole-market awareness;
- exchangeInfo cached for 24h;
- dynamic active universe: 24 / 36 / 48 symbols;
- lanes: CORE, MOMENTUM, HYPE;
- 15m mature scoring only once per closed 15m bucket;
- 1m HYPE scoring every pulse, with radar-only fallback for <60m listings;
- 85%+ API capacity buffer via conservative internal thresholds;
- 429 -> circuit cooldown, 418 -> persistent smart-scanner disable;
- single-instance flock;
- no automatic symbol promotion into LIVE.
"""
from __future__ import annotations

import argparse
import bisect
try:
    import fcntl  # Linux/Unix single-instance lock; runtime target is the VPS.
except ModuleNotFoundError:  # Allows Windows import/unit tests without weakening Linux runtime.
    fcntl = None  # type: ignore[assignment]
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "pulse_minutes": 5,
    "baseline_refresh_hours": 24,
    "exchange_info_refresh_hours": 24,
    "min_active": 24,
    "target_active": 36,
    "max_active": 48,
    "watchlist_size": 64,
    "core_slots": 8,
    "momentum_slots": 16,
    "hype_slots": 12,
    "green_used_weight": 300,
    "caution_used_weight": 600,
    "pause_used_weight": 750,
    "absolute_scanner_ceiling": 900,
    "min_core_quote_volume_usdt": 1_000_000,
    "min_momentum_quote_volume_usdt": 250_000,
    "min_hype_quote_volume_usdt": 50_000,
    "max_core_spread_pct": 0.35,
    "max_hype_spread_pct": 0.80,
    "hype_score_threshold": 62.0,
    "momentum_score_threshold": 56.0,
    "urgent_hype_score": 82.0,
    "alert_cooldown_minutes": 30,
    "max_alerts_per_cycle": 2,
    "handoff_configured_top": True,
    "stable_bases": ["USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1"],
    "exclude_base_suffixes": ["UP", "DOWN", "BULL", "BEAR"],
}


class SmartScannerError(RuntimeError):
    pass


class RateLimitError(SmartScannerError):
    def __init__(self, status: int, retry_after: int | None = None, message: str | None = None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(message or f"Binance returned HTTP {status}")


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last: float
    open: float
    high: float
    low: float
    bid: float
    ask: float
    quote_volume: float
    trades: int
    change_pct_24h: float
    close_time: int

    @property
    def spread_pct(self) -> float:
        if self.bid <= 0 or self.ask <= 0:
            return 999.0
        mid = (self.bid + self.ask) / 2.0
        return (self.ask - self.bid) / mid * 100.0 if mid > 0 else 999.0

    @property
    def range_pct(self) -> float:
        base = self.open if self.open > 0 else self.last
        return ((self.high - self.low) / base * 100.0) if base > 0 and self.high >= self.low else 0.0


@dataclass
class Feature:
    ticker: Ticker
    base_asset: str
    new_symbol: bool = False
    ret_5m: float = 0.0
    ret_15m: float = 0.0
    ret_60m: float = 0.0
    quote_delta_5m: float = 0.0
    quote_delta_15m: float = 0.0
    quote_delta_60m: float = 0.0
    trades_delta_5m: float = 0.0
    trades_delta_15m: float = 0.0
    trades_delta_60m: float = 0.0
    core_score: float = 0.0
    momentum_score: float = 0.0
    hype_score: float = 0.0
    composite_score: float = 0.0
    lane: str | None = None


@dataclass
class ScanScore:
    symbol: str
    interval: str
    native_score: float | None
    candidate_eligible: bool
    classification: str
    candle_close_time: int | None
    reason: str | None = None


class BinancePublicClient:
    def __init__(self, base_url: str, timeout: int = 10) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.used_weight_1m = 0
        self.request_count = 0

    @staticmethod
    def _weight_from_headers(headers: Any) -> int | None:
        for key in ("X-MBX-USED-WEIGHT-1M", "x-mbx-used-weight-1m"):
            value = headers.get(key) if headers is not None else None
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
        return None

    def get_json(self, path: str, params: dict[str, str] | None = None, *, retry_5xx: bool = True) -> Any:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "RiskPilot-Smart-Radar/1.0",
        }, method="GET")
        attempts = 2 if retry_5xx else 1
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    self.request_count += 1
                    used = self._weight_from_headers(response.headers)
                    if used is not None:
                        self.used_weight_1m = used
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                self.request_count += 1
                used = self._weight_from_headers(exc.headers)
                if used is not None:
                    self.used_weight_1m = used
                retry_raw = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    retry_after = int(retry_raw) if retry_raw is not None else None
                except ValueError:
                    retry_after = None
                if exc.code in {418, 429}:
                    raise RateLimitError(exc.code, retry_after) from exc
                if 500 <= exc.code <= 599 and attempt + 1 < attempts:
                    time.sleep(1.5 + random.random() * 1.5)
                    last = exc
                    continue
                raise SmartScannerError(f"Binance market API returned HTTP {exc.code}") from exc
            except urllib.error.URLError as exc:
                raise SmartScannerError(f"Binance market API unreachable: {exc.reason}") from exc
            except (TimeoutError, json.JSONDecodeError) as exc:
                raise SmartScannerError(f"invalid Binance market response: {exc}") from exc
        raise SmartScannerError(str(last or "Binance request failed"))


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default


def load_smart_config(path: Path) -> dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = read_json(path, {})
    if raw:
        if not isinstance(raw, dict):
            raise SmartScannerError("smart scanner config must be a JSON object")
        unknown = sorted(set(raw) - set(DEFAULTS))
        if unknown:
            raise SmartScannerError(f"unknown smart scanner config keys: {', '.join(unknown)}")
        cfg.update(raw)
    if not (1 <= int(cfg["min_active"]) <= int(cfg["target_active"]) <= int(cfg["max_active"]) <= 100):
        raise SmartScannerError("active universe sizes must satisfy 1 <= min <= target <= max <= 100")
    if not (0 < int(cfg["green_used_weight"]) < int(cfg["caution_used_weight"]) < int(cfg["pause_used_weight"]) < int(cfg["absolute_scanner_ceiling"]) < 6000):
        raise SmartScannerError("smart rate thresholds must be ordered below 6000")
    if int(cfg["watchlist_size"]) < int(cfg["max_active"]):
        raise SmartScannerError("watchlist_size must be >= max_active")
    return cfg


def pct_rank(value: float, sorted_values: list[float]) -> float:
    if not sorted_values:
        return 0.0
    idx = bisect.bisect_right(sorted_values, value)
    return 100.0 * idx / len(sorted_values)


def safe_log(value: float) -> float:
    return math.log10(max(value, 1.0))


def price_return(now: float, prior: float) -> float:
    return ((now / prior) - 1.0) * 100.0 if now > 0 and prior > 0 else 0.0


def _sample_at(samples: list[dict[str, Any]], target_ts: float) -> dict[str, Any] | None:
    best = None
    for item in samples:
        try:
            ts = float(item["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if ts <= target_ts:
            best = item
        else:
            break
    return best


def build_features(
    tickers: list[Ticker],
    metadata: dict[str, dict[str, Any]],
    history: dict[str, list[dict[str, Any]]],
    baseline_symbols: set[str],
    now_ts: float,
) -> list[Feature]:
    features: list[Feature] = []
    for ticker in tickers:
        meta = metadata.get(ticker.symbol)
        if not meta:
            continue
        base = str(meta.get("baseAsset", ""))
        samples = history.get(ticker.symbol, [])
        p5 = _sample_at(samples, now_ts - 5 * 60)
        p15 = _sample_at(samples, now_ts - 15 * 60)
        p60 = _sample_at(samples, now_ts - 60 * 60)
        feat = Feature(ticker=ticker, base_asset=base, new_symbol=bool(baseline_symbols and ticker.symbol not in baseline_symbols))
        for prior, suffix in ((p5, "5m"), (p15, "15m"), (p60, "60m")):
            if prior is None:
                continue
            old_price = float(prior.get("p", 0.0) or 0.0)
            old_quote = float(prior.get("q", 0.0) or 0.0)
            old_trades = int(prior.get("n", 0) or 0)
            setattr(feat, f"ret_{suffix}", price_return(ticker.last, old_price))
            setattr(feat, f"quote_delta_{suffix}", max(0.0, ticker.quote_volume - old_quote))
            setattr(feat, f"trades_delta_{suffix}", max(0.0, float(ticker.trades - old_trades)))
        features.append(feat)
    return features


def score_features(features: list[Feature]) -> None:
    if not features:
        return
    logs_q = sorted(safe_log(f.ticker.quote_volume) for f in features)
    logs_n = sorted(safe_log(float(f.ticker.trades)) for f in features)
    spreads = sorted(f.ticker.spread_pct for f in features)
    ranges = sorted(f.ticker.range_pct for f in features)
    r5 = sorted(max(0.0, f.ret_5m) for f in features)
    r15 = sorted(max(0.0, f.ret_15m) for f in features)
    q5 = sorted(safe_log(f.quote_delta_5m) for f in features)
    q15 = sorted(safe_log(f.quote_delta_15m) for f in features)
    n5 = sorted(safe_log(f.trades_delta_5m) for f in features)
    n15 = sorted(safe_log(f.trades_delta_15m) for f in features)
    for f in features:
        liq = pct_rank(safe_log(f.ticker.quote_volume), logs_q)
        trades = pct_rank(safe_log(float(f.ticker.trades)), logs_n)
        spread_quality = 100.0 - pct_rank(f.ticker.spread_pct, spreads)
        range_rank = pct_rank(f.ticker.range_pct, ranges)
        ret5 = pct_rank(max(0.0, f.ret_5m), r5)
        ret15 = pct_rank(max(0.0, f.ret_15m), r15)
        q5r = pct_rank(safe_log(f.quote_delta_5m), q5)
        q15r = pct_rank(safe_log(f.quote_delta_15m), q15)
        n5r = pct_rank(safe_log(f.trades_delta_5m), n5)
        n15r = pct_rank(safe_log(f.trades_delta_15m), n15)
        stability = max(0.0, 100.0 - min(abs(f.ticker.change_pct_24h), 30.0) / 30.0 * 100.0)
        f.core_score = 0.48 * liq + 0.22 * trades + 0.20 * spread_quality + 0.10 * stability
        f.momentum_score = 0.28 * ret15 + 0.24 * q15r + 0.18 * n15r + 0.16 * range_rank + 0.14 * liq
        f.hype_score = 0.28 * q5r + 0.20 * n5r + 0.20 * ret5 + 0.12 * ret15 + 0.10 * range_rank + 0.10 * liq
        if f.new_symbol:
            f.hype_score = min(100.0, f.hype_score + 12.0)
        # A single parabolic candle or very wide spread is interesting but not
        # automatically good. Penalize rather than silently exclude.
        if f.ret_5m > 12.0:
            f.hype_score *= 0.82
            f.momentum_score *= 0.90
        if f.ticker.spread_pct > 0.80:
            f.hype_score *= 0.65
            f.momentum_score *= 0.70
        f.composite_score = max(f.core_score * 0.85, f.momentum_score, f.hype_score)


def market_heat(features: Iterable[Feature]) -> int:
    return sum(1 for f in features if f.hype_score >= 72.0 or f.momentum_score >= 72.0)


def choose_active_count(cfg: dict[str, Any], features: list[Feature], used_weight: int) -> int:
    if used_weight >= int(cfg["pause_used_weight"]):
        return 0
    cap = int(cfg["max_active"])
    if used_weight >= int(cfg["caution_used_weight"]):
        cap = int(cfg["min_active"])
    elif used_weight >= int(cfg["green_used_weight"]):
        cap = int(cfg["target_active"])
    heat = market_heat(features)
    desired = int(cfg["max_active"]) if heat >= 18 else (int(cfg["min_active"]) if heat <= 5 else int(cfg["target_active"]))
    return min(cap, desired)


def lane_slots(active_count: int, cfg: dict[str, Any]) -> tuple[int, int, int]:
    if active_count <= 0:
        return (0, 0, 0)
    if active_count <= 24:
        return (min(6, active_count), min(10, max(0, active_count - 6)), max(0, active_count - min(6, active_count) - min(10, max(0, active_count - 6))))
    if active_count >= 48:
        return (8, 22, active_count - 30)
    # target 36; proportional interpolation for unusual values in between.
    core = min(8, max(6, round(active_count * 8 / 36)))
    hype = min(18, max(8, round(active_count * 12 / 36)))
    momentum = max(0, active_count - core - hype)
    return core, momentum, hype


def select_universe(features: list[Feature], cfg: dict[str, Any], active_count: int) -> dict[str, list[Feature]]:
    eligible = [f for f in features if f.ticker.last > 0 and f.ticker.bid > 0 and f.ticker.ask > 0]
    watch = sorted(eligible, key=lambda f: (f.composite_score, f.ticker.quote_volume), reverse=True)[: int(cfg["watchlist_size"])]
    core_slots, momentum_slots, hype_slots = lane_slots(active_count, cfg)
    selected: set[str] = set()

    core_pool = [f for f in eligible if f.ticker.quote_volume >= float(cfg["min_core_quote_volume_usdt"])
                 and f.ticker.spread_pct <= float(cfg["max_core_spread_pct"])]
    core = sorted(core_pool, key=lambda f: (f.core_score, f.ticker.quote_volume), reverse=True)[:core_slots]
    for f in core:
        f.lane = "CORE"; selected.add(f.ticker.symbol)

    hype_pool = [f for f in eligible if f.ticker.symbol not in selected
                 and f.ticker.quote_volume >= float(cfg["min_hype_quote_volume_usdt"])
                 and f.ticker.spread_pct <= float(cfg["max_hype_spread_pct"])
                 and (f.hype_score >= float(cfg["hype_score_threshold"]) or f.new_symbol)]
    hype = sorted(hype_pool, key=lambda f: (f.hype_score, f.ret_5m, f.quote_delta_5m), reverse=True)[:hype_slots]
    for f in hype:
        f.lane = "HYPE"; selected.add(f.ticker.symbol)

    momentum_pool = [f for f in eligible if f.ticker.symbol not in selected
                     and f.ticker.quote_volume >= float(cfg["min_momentum_quote_volume_usdt"])
                     and f.momentum_score >= float(cfg["momentum_score_threshold"])]
    momentum = sorted(momentum_pool, key=lambda f: (f.momentum_score, f.ticker.quote_volume), reverse=True)[:momentum_slots]
    for f in momentum:
        f.lane = "MOMENTUM"; selected.add(f.ticker.symbol)

    active = core + momentum + hype
    if len(active) < active_count:
        remaining = [f for f in watch if f.ticker.symbol not in selected]
        for f in remaining[: active_count - len(active)]:
            f.lane = f.lane or "MOMENTUM"
            active.append(f); selected.add(f.ticker.symbol)
    return {"core": core, "momentum": momentum, "hype": hype, "active": active[:active_count], "watchlist": watch}


def parse_tickers(payload: Any, metadata: dict[str, dict[str, Any]], cfg: dict[str, Any]) -> list[Ticker]:
    if not isinstance(payload, list):
        raise SmartScannerError("Binance all-ticker payload is not an array")
    stable = {str(x).upper() for x in cfg["stable_bases"]}
    suffixes = tuple(str(x).upper() for x in cfg["exclude_base_suffixes"])
    result: list[Ticker] = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "")).upper()
        meta = metadata.get(symbol)
        if not meta or str(meta.get("status")) != "TRADING" or str(meta.get("quoteAsset")) != "USDT" or not bool(meta.get("isSpotTradingAllowed", False)):
            continue
        base = str(meta.get("baseAsset", "")).upper()
        if base in stable or base.endswith(suffixes):
            continue
        try:
            ticker = Ticker(
                symbol=symbol,
                last=float(row["lastPrice"]), open=float(row["openPrice"]),
                high=float(row["highPrice"]), low=float(row["lowPrice"]),
                bid=float(row.get("bidPrice", 0) or 0), ask=float(row.get("askPrice", 0) or 0),
                quote_volume=float(row["quoteVolume"]), trades=int(row.get("count", 0) or 0),
                change_pct_24h=float(row.get("priceChangePercent", 0) or 0),
                close_time=int(row.get("closeTime", int(time.time() * 1000))),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if min(ticker.last, ticker.open, ticker.high, ticker.low) <= 0 or ticker.quote_volume < 0 or ticker.trades < 0:
            continue
        result.append(ticker)
    return result


def update_history(path: Path, tickers: list[Ticker], now_ts: float, max_age_seconds: int = 70 * 60) -> dict[str, list[dict[str, Any]]]:
    raw = read_json(path, {"samples": {}})
    samples = raw.get("samples", {}) if isinstance(raw, dict) else {}
    if not isinstance(samples, dict):
        samples = {}
    cutoff = now_ts - max_age_seconds
    for t in tickers:
        rows = samples.get(t.symbol, [])
        if not isinstance(rows, list):
            rows = []
        rows = [r for r in rows if isinstance(r, dict) and float(r.get("t", 0) or 0) >= cutoff]
        rows.append({"t": now_ts, "p": t.last, "q": t.quote_volume, "n": t.trades})
        samples[t.symbol] = rows[-15:]
    # Drop symbols not seen for >70m so the state cannot grow indefinitely.
    for symbol in list(samples):
        rows = samples[symbol]
        if not rows or float(rows[-1].get("t", 0) or 0) < cutoff:
            samples.pop(symbol, None)
    atomic_write_json(path, {"updated_at": now_ts, "samples": samples})
    return samples


def interval_ms(interval: str) -> int:
    unit = interval[-1]
    number = int(interval[:-1])
    return number * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]


def score_kline_payload(project_root: Path, base_settings: Any, symbol: str, interval: str, rows: Any, expanded_symbols: list[str]) -> ScanScore:
    if str(project_root / "src") not in sys.path:
        sys.path.insert(0, str(project_root / "src"))
    from spotguard.indicators import analyze
    from spotguard.market import normalize_klines
    from spotguard.score_engine import score_snapshot
    from spotguard.strategy import evaluate

    if not isinstance(rows, list):
        return ScanScore(symbol, interval, None, False, "INVALID", None, "kline payload is not an array")
    try:
        klines = normalize_klines(rows, drop_open_candle=True, interval_ms=interval_ms(interval))
    except Exception as exc:
        return ScanScore(symbol, interval, None, False, "EMERGING_RADAR", None, str(exc))
    market = replace(base_settings.market, symbols=tuple(expanded_symbols), interval=interval)
    settings = replace(base_settings, market=market)
    snapshot = analyze(klines)
    scored = score_snapshot(settings, symbol, snapshot, klines[-2].close)
    signal = evaluate(settings, symbol, snapshot)
    return ScanScore(symbol=symbol, interval=interval, native_score=float(scored.native_total_score),
                     candidate_eligible=bool(signal and scored.candidate_eligible),
                     classification=str(scored.market_signal_classification),
                     candle_close_time=klines[-1].close_time,
                     reason=scored.candidate_ineligibility_reason)


def feature_dict(f: Feature) -> dict[str, Any]:
    return {
        "symbol": f.ticker.symbol, "lane": f.lane,
        "core_score": round(f.core_score, 2), "momentum_score": round(f.momentum_score, 2),
        "hype_score": round(f.hype_score, 2), "composite_score": round(f.composite_score, 2),
        "quote_volume_24h": round(f.ticker.quote_volume, 2), "spread_pct": round(f.ticker.spread_pct, 4),
        "ret_5m_pct": round(f.ret_5m, 3), "ret_15m_pct": round(f.ret_15m, 3),
        "quote_delta_5m": round(f.quote_delta_5m, 2), "trades_delta_5m": int(f.trades_delta_5m),
        "new_symbol": f.new_symbol,
    }


def top_radar_rows(features: list[Feature], scores: dict[str, ScanScore], *, limit: int = 5) -> list[dict[str, Any]]:
    """Rank cross-lane observation candidates without creating a trade signal.

    The lane scores are deliberately blended so a transient HYPE pulse cannot
    monopolize the read-only view.  A current closed 15m canonical score is a
    confirmation bonus, not a replacement for the existing candidate flow.
    """
    rows: list[dict[str, Any]] = []
    for feature in features:
        score = scores.get(feature.ticker.symbol)
        potential = 0.40 * feature.core_score + 0.35 * feature.momentum_score + 0.25 * feature.hype_score
        native_score = score.native_score if score else None
        if score and score.interval == "15m" and native_score is not None:
            potential += 10.0 if score.candidate_eligible else min(5.0, native_score / 20.0)
        elif score and score.interval == "1m" and score.candidate_eligible:
            potential += 3.0
        if feature.ret_5m * feature.ret_15m < 0:
            potential -= 5.0
        label = "POTENSI"
        if score and score.interval == "15m" and score.candidate_eligible:
            label = "POTENSI_TINGGI"
        elif score and score.interval == "1m" and score.candidate_eligible:
            label = "EMERGING"
        rows.append({
            "symbol": feature.ticker.symbol,
            "lane": feature.lane or "WATCH",
            "potential_score": round(max(0.0, potential), 2),
            "label": label,
            "configured": False,  # Filled by the caller; never changes the allowlist.
            "native_score": round(native_score, 2) if native_score is not None else None,
            "native_interval": score.interval if score and native_score is not None else None,
            "candidate_eligible": bool(score and score.candidate_eligible),
            "core_score": round(feature.core_score, 2),
            "momentum_score": round(feature.momentum_score, 2),
            "hype_score": round(feature.hype_score, 2),
            "ret_5m_pct": round(feature.ret_5m, 3),
            "ret_15m_pct": round(feature.ret_15m, 3),
            "spread_pct": round(feature.ticker.spread_pct, 4),
        })
    rows.sort(key=lambda row: (-float(row["potential_score"]), str(row["symbol"])))
    return rows[:limit]


def circuit_check(path: Path, now_ts: float) -> dict[str, Any]:
    state = read_json(path, {})
    return state if isinstance(state, dict) else {}


def trip_circuit(path: Path, exc: RateLimitError, now_ts: float) -> None:
    if exc.status == 418:
        atomic_write_json(path, {"disabled": True, "reason": "HTTP 418 IP ban", "tripped_at": now_ts})
        return
    retry = max(60, int(exc.retry_after or 600))
    atomic_write_json(path, {"disabled": False, "reason": "HTTP 429", "tripped_at": now_ts,
                             "retry_after": retry, "cooldown_until": now_ts + retry})


def maybe_refresh_exchange_info(client: BinancePublicClient, state_dir: Path, cfg: dict[str, Any], now_ts: float) -> dict[str, dict[str, Any]]:
    path = state_dir / "smart-exchange-info.json"
    cached = read_json(path, {})
    age = now_ts - float(cached.get("fetched_at", 0) or 0) if isinstance(cached, dict) else 10**12
    if isinstance(cached, dict) and isinstance(cached.get("symbols"), list) and age < int(cfg["exchange_info_refresh_hours"]) * 3600:
        rows = cached["symbols"]
    else:
        payload = client.get_json("/api/v3/exchangeInfo", {})
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise SmartScannerError("exchangeInfo payload is invalid")
        rows = payload["symbols"]
        atomic_write_json(path, {"fetched_at": now_ts, "symbols": rows})
    return {str(row.get("symbol", "")): row for row in rows if isinstance(row, dict)}


def load_or_create_baseline(path: Path, tickers: list[Ticker], cfg: dict[str, Any], now_ts: float) -> set[str]:
    current = {t.symbol for t in tickers}
    raw = read_json(path, {})
    if not isinstance(raw, dict) or not isinstance(raw.get("symbols"), list):
        atomic_write_json(path, {"fetched_at": now_ts, "symbols": sorted(current)})
        return current  # First boot is not treated as hundreds of new listings.
    symbols = set(str(x) for x in raw["symbols"])
    if now_ts - float(raw.get("fetched_at", 0) or 0) >= int(cfg["baseline_refresh_hours"]) * 3600:
        # Score against the old baseline first; caller writes the refreshed
        # baseline only after this cycle's feature extraction.
        return symbols
    return symbols


def refresh_baseline_if_due(path: Path, tickers: list[Ticker], cfg: dict[str, Any], now_ts: float) -> None:
    raw = read_json(path, {})
    age = now_ts - float(raw.get("fetched_at", 0) or 0) if isinstance(raw, dict) else 10**12
    if age >= int(cfg["baseline_refresh_hours"]) * 3600:
        atomic_write_json(path, {"fetched_at": now_ts, "symbols": sorted(t.symbol for t in tickers)})


def run(args: argparse.Namespace) -> dict[str, Any]:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root / "src") not in sys.path:
        sys.path.insert(0, str(project_root / "src"))
    from spotguard.config import default_config_path, load_settings
    from spotguard.service import SpotGuard

    smart_cfg_path = Path(args.smart_config).expanduser().resolve() if args.smart_config else project_root / "smart-scanner.json"
    cfg = load_smart_config(smart_cfg_path)
    if not cfg["enabled"]:
        return {"ok": True, "enabled": False, "reason": "smart scanner disabled in smart-scanner.json"}
    settings = load_settings(Path(args.config).expanduser() if args.config else default_config_path())
    state_dir = settings.state_dir / "smart-scanner"
    state_dir.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        raise SmartScannerError("smart scanner runtime requires Linux/Unix fcntl locking; Windows is supported for import/unit tests only")
    lock_file = (state_dir / "scanner.lock").open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"ok": True, "skipped": True, "reason": "another smart-scanner instance is still running"}

    now_ts = time.time()
    circuit = state_dir / "circuit.json"
    circuit_state = circuit_check(circuit, now_ts)
    if circuit_state.get("disabled"):
        return {"ok": True, "skipped": True, "circuit_disabled": True,
                "reason": circuit_state.get("reason", "HTTP 418 circuit breaker")}
    cooldown_until = float(circuit_state.get("cooldown_until", 0) or 0)
    if cooldown_until > now_ts:
        return {"ok": True, "skipped": True, "cooldown": True,
                "cooldown_until": datetime.fromtimestamp(cooldown_until, timezone.utc).isoformat(),
                "reason": circuit_state.get("reason", "HTTP 429 cooldown")}
    client = BinancePublicClient(settings.market.base_url, settings.market.request_timeout_seconds)
    try:
        metadata = maybe_refresh_exchange_info(client, state_dir, cfg, now_ts)
        tick_payload = client.get_json("/api/v3/ticker/24hr", {})
        tickers = parse_tickers(tick_payload, metadata, cfg)
    except RateLimitError as exc:
        trip_circuit(circuit, exc, now_ts)
        raise

    baseline_path = state_dir / "baseline-24h.json"
    baseline_symbols = load_or_create_baseline(baseline_path, tickers, cfg, now_ts)
    history_path = state_dir / "pulse-history.json"
    old_history = read_json(history_path, {"samples": {}})
    history_before = old_history.get("samples", {}) if isinstance(old_history, dict) and isinstance(old_history.get("samples"), dict) else {}
    features = build_features(tickers, metadata, history_before, baseline_symbols, now_ts)
    score_features(features)
    history_after = update_history(history_path, tickers, now_ts)
    refresh_baseline_if_due(baseline_path, tickers, cfg, now_ts)

    active_count = choose_active_count(cfg, features, client.used_weight_1m)
    universe = select_universe(features, cfg, active_count)
    atomic_write_json(state_dir / "active-universe.json", {
        "generated_at": now_ts,
        "used_weight_1m": client.used_weight_1m,
        "active_count": active_count,
        "core": [feature_dict(f) for f in universe["core"]],
        "momentum": [feature_dict(f) for f in universe["momentum"]],
        "hype": [feature_dict(f) for f in universe["hype"]],
        "active": [feature_dict(f) for f in universe["active"]],
        "watchlist": [feature_dict(f) for f in universe["watchlist"]],
    })

    if active_count == 0:
        return {"ok": True, "radar_only": True, "reason": "API weight in conservative pause zone",
                "used_weight_1m": client.used_weight_1m, "eligible_symbols": len(features)}

    expanded = sorted(set(settings.market.symbols) | {f.ticker.symbol for f in universe["active"]})
    scores: dict[str, ScanScore] = {}
    request_budget_stop = False

    # HYPE lane: 1m scoring each pulse. New listings with <60 closed 1m candles
    # remain visible as radar-only rather than failing the whole cycle.
    for f in universe["hype"]:
        if client.used_weight_1m >= int(cfg["pause_used_weight"]):
            request_budget_stop = True; break
        try:
            rows = client.get_json("/api/v3/klines", {"symbol": f.ticker.symbol, "interval": "1m", "limit": "61"})
            scores[f.ticker.symbol] = score_kline_payload(project_root, settings, f.ticker.symbol, "1m", rows, expanded)
        except RateLimitError as exc:
            trip_circuit(circuit, exc, time.time()); raise
        except SmartScannerError as exc:
            scores[f.ticker.symbol] = ScanScore(f.ticker.symbol, "1m", None, False, "ERROR", None, str(exc))

    # Mature lane: fetch only once per 15m bucket despite the 5m timer.
    schedule_path = state_dir / "schedule.json"
    schedule = read_json(schedule_path, {})
    current_15m_bucket = int(now_ts // (15 * 60))
    mature_due = int(schedule.get("last_mature_bucket", -1) or -1) != current_15m_bucket
    if mature_due and not request_budget_stop:
        for f in universe["core"] + universe["momentum"]:
            if client.used_weight_1m >= int(cfg["pause_used_weight"]):
                request_budget_stop = True; break
            try:
                rows = client.get_json("/api/v3/klines", {"symbol": f.ticker.symbol, "interval": settings.market.interval, "limit": "61"})
                scores[f.ticker.symbol] = score_kline_payload(project_root, settings, f.ticker.symbol, settings.market.interval, rows, expanded)
            except RateLimitError as exc:
                trip_circuit(circuit, exc, time.time()); raise
            except SmartScannerError as exc:
                scores[f.ticker.symbol] = ScanScore(f.ticker.symbol, settings.market.interval, None, False, "ERROR", None, str(exc))
        if not request_budget_stop:
            schedule["last_mature_bucket"] = current_15m_bucket
            schedule["updated_at"] = now_ts
            atomic_write_json(schedule_path, schedule)

    # Rank using native score when it exists; otherwise HYPE radar score keeps
    # first-hour coins visible without pretending they passed canonical scoring.
    by_symbol = {f.ticker.symbol: f for f in universe["active"]}
    ranked = sorted(universe["active"], key=lambda f: (
        scores.get(f.ticker.symbol).native_score if scores.get(f.ticker.symbol) and scores[f.ticker.symbol].native_score is not None else f.hype_score,
        f.composite_score,
    ), reverse=True)

    # Passive Radar observations are intentionally persisted, not pushed to
    # Telegram.  This keeps a five-minute pulse from becoming notification
    # spam; only the existing candidate/proposal flow below may notify.
    radar_rows = top_radar_rows(universe["active"], scores)
    for row in radar_rows:
        row["configured"] = row["symbol"] in settings.market.symbols
    atomic_write_json(state_dir / "top-radar.json", {
        "generated_at": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "active_count": active_count,
        "used_weight_1m": client.used_weight_1m,
        "rows": radar_rows,
        "notice": "Read-only potential ranking; it is not a candidate, proposal, or entry instruction.",
    })

    # Preserve the old flow exactly: if the strongest qualifying smart symbol is
    # already in the existing configured allowlist, hand only that symbol to the
    # existing scanner. Dynamic/unconfigured symbols never reach proposal/LIVE.
    handoff = None
    if bool(cfg["handoff_configured_top"]) and args.notify:
        configured_ranked = [f for f in ranked if f.ticker.symbol in settings.market.symbols]
        top = next((f for f in configured_ranked if scores.get(f.ticker.symbol) and scores[f.ticker.symbol].candidate_eligible), None)
        if top is not None:
            handoff = SpotGuard(settings).scan(symbols=[top.ticker.symbol], notify=args.notify, dry_run=not args.notify)

    return {
        "ok": True,
        "whole_market_symbols": len(tick_payload) if isinstance(tick_payload, list) else None,
        "eligible_usdt_spot_symbols": len(features),
        "watchlist_size": len(universe["watchlist"]),
        "active_count": active_count,
        "lanes": {"core": [f.ticker.symbol for f in universe["core"]],
                  "momentum": [f.ticker.symbol for f in universe["momentum"]],
                  "hype": [f.ticker.symbol for f in universe["hype"]]},
        "used_weight_1m": client.used_weight_1m,
        "request_count": client.request_count,
        "mature_scan_due": mature_due,
        "budget_paused_mid_cycle": request_budget_stop,
        "alerts": [],
        "radar_notifications_suppressed": True,
        "top_radar": radar_rows,
        "existing_flow_handoff": handoff,
        "state_dir": str(state_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="RiskPilot additive smart-universe scanner")
    parser.add_argument("--config", help="existing RiskPilot config.json; defaults to RiskPilot resolution")
    parser.add_argument("--smart-config", help="smart-scanner.json path")
    parser.add_argument("--notify", action="store_true", help="send only existing candidate/proposal notifications; passive Radar remains read-only")
    parser.add_argument("--json", action="store_true", help="compact JSON output")
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":") if args.json else None, indent=None if args.json else 2))
        return 0
    except RateLimitError as exc:
        print(json.dumps({"ok": False, "type": "RateLimitError", "status": exc.status,
                          "retry_after": exc.retry_after, "error": str(exc)}))
        return 75
    except SmartScannerError as exc:
        print(json.dumps({"ok": False, "type": type(exc).__name__, "error": str(exc)}))
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "type": type(exc).__name__, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
