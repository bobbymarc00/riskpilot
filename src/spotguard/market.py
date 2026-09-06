from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from decimal import Decimal

from .config import Settings


class MarketError(RuntimeError):
    pass


class SymbolValidationError(MarketError):
    pass

@dataclass(frozen=True)
class SpotMarketSnapshot:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    min_notional: Decimal
    step_size: Decimal
    status: str
    observed_at_ms: int
    price_tick_size: Decimal = Decimal("0.00000001")


def _public_json(settings: Settings, path: str, params: dict[str, str]) -> Any:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(f"{settings.market.base_url}{path}?{query}", headers={"Accept": "application/json", "User-Agent": "RiskPilot-Agent-OS/1.0"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=settings.market.request_timeout_seconds) as response:
            if response.status != 200:
                raise MarketError(f"Binance market API returned HTTP {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MarketError(f"Binance market API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MarketError(f"Binance market API is unreachable: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise MarketError(f"invalid response from Binance market API: {exc}") from exc


def validate_spot_symbol(settings: Settings, symbol: str) -> dict[str, Any]:
    if symbol not in settings.market.symbols:
        raise SymbolValidationError(f"symbol is not configured: {symbol}")
    try:
        info = _public_json(settings, "/api/v3/exchangeInfo", {"symbol": symbol})
    except MarketError as exc:
        raise SymbolValidationError(f"{symbol} exchangeInfo validation failed: {exc}") from exc
    try:
        items = info["symbols"]
        if len(items) != 1:
            raise KeyError("symbols")
        item = items[0]
        filters = {f["filterType"]: f for f in item["filters"]}
        lot = filters["LOT_SIZE"]
        market_lot = filters.get("MARKET_LOT_SIZE", lot)
        notional = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
        if notional is None:
            raise KeyError("MIN_NOTIONAL")
        lot_step = Decimal(str(lot["stepSize"]))
        lot_min = Decimal(str(lot["minQty"])); lot_max = Decimal(str(lot["maxQty"]))
        market_step = Decimal(str(market_lot["stepSize"]))
        market_min = Decimal(str(market_lot.get("minQty", "0"))); market_max = Decimal(str(market_lot.get("maxQty", "0")))
        effective_step = market_step if market_step > 0 else lot_step
        minimum = Decimal(str(notional["minNotional"]))
        status = str(item["status"])
        quote = str(item["quoteAsset"])
        spot_allowed = bool(item.get("isSpotTradingAllowed", False))
        price_filter = filters.get("PRICE_FILTER")
        price_tick = Decimal(str(price_filter["tickSize"])) if price_filter else Decimal("0")
        percent_filter = filters.get("PERCENT_PRICE_BY_SIDE", filters.get("PERCENT_PRICE"))
        live_flags = {"oto_allowed": bool(item.get("otoAllowed", False)),
            "opo_allowed": bool(item.get("opoAllowed", False)), "oco_allowed": bool(item.get("ocoAllowed", False))}
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise SymbolValidationError(f"{symbol} exchangeInfo filters are invalid") from exc
    if status != "TRADING":
        raise SymbolValidationError(f"{symbol} status is not TRADING")
    if quote != settings.risk.quote_asset:
        raise SymbolValidationError(f"{symbol} quote asset is not {settings.risk.quote_asset}")
    if not spot_allowed:
        raise SymbolValidationError(f"{symbol} Spot trading is not permitted")
    # Binance uses stepSize=0 to mean MARKET_LOT_SIZE imposes no separate step;
    # LOT_SIZE remains authoritative even when MARKET_LOT_SIZE maxQty is populated.
    market_lot_valid = market_step == 0 or (market_step > 0 and market_min >= 0 and market_max >= market_min)
    if lot_step <= 0 or lot_min <= 0 or lot_max < lot_min or not market_lot_valid or effective_step <= 0 or minimum <= 0:
        raise SymbolValidationError(f"{symbol} Spot quantity/notional filters are invalid")
    return {"symbol": symbol, "status": status, "quote_asset": quote,
        "spot_trading_allowed": True, "lot_step_size": str(lot_step),
        "market_step_size": str(effective_step), "min_notional": str(minimum),
        "price_tick_size": str(price_tick), "percent_price_filter": bool(percent_filter),
        "max_num_orders": int(filters.get("MAX_NUM_ORDERS", {}).get("maxNumOrders", 0)),
        "max_num_algo_orders": int(filters.get("MAX_NUM_ALGO_ORDERS", {}).get("maxNumAlgoOrders", 0)),
        "max_num_order_lists": int(filters.get("MAX_NUM_ORDER_LISTS", {}).get("maxNumOrderLists", 0)), **live_flags}


def fetch_spot_snapshot(settings: Settings, symbol: str) -> SpotMarketSnapshot:
    validation = validate_spot_symbol(settings, symbol)
    book = _public_json(settings, "/api/v3/ticker/bookTicker", {"symbol": symbol})
    price = _public_json(settings, "/api/v3/ticker/price", {"symbol": symbol})
    try:
        result = SpotMarketSnapshot(symbol=symbol, bid=Decimal(str(book["bidPrice"])),
            ask=Decimal(str(book["askPrice"])), last=Decimal(str(price["price"])),
            min_notional=Decimal(validation["min_notional"]),
            step_size=Decimal(validation["market_step_size"]), status=validation["status"],
            observed_at_ms=int(time.time() * 1000), price_tick_size=Decimal(validation["price_tick_size"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketError("Binance ticker payload is invalid") from exc
    if min(result.bid, result.ask, result.last, result.step_size) <= 0 or result.ask < result.bid:
        raise MarketError("public Spot prices are invalid")
    return result


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    return (quantity / step).to_integral_value(rounding="ROUND_FLOOR") * step

ANALYSIS_CANDLE_COUNT = 60
PREFILTER_REQUEST_COUNT = ANALYSIS_CANDLE_COUNT + 1

def _interval_ms(interval: str) -> int:
    unit = interval[-1]
    magnitude = int(interval[:-1])
    return magnitude * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[unit]



@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_kline(row: Any) -> Kline:
    if isinstance(row, dict):
        try:
            return Kline(
                open_time=int(row["open_time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                close_time=int(row["close_time"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MarketError("invalid object kline") from exc
    if not isinstance(row, list) or len(row) < 7:
        raise MarketError("invalid Binance kline row")
    try:
        return Kline(
            open_time=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
            close_time=int(row[6]),
        )
    except (TypeError, ValueError) as exc:
        raise MarketError("invalid numeric value in kline") from exc


def normalize_klines(rows: Iterable[Any], drop_open_candle: bool = True,
                     interval_ms: int | None = None) -> list[Kline]:
    parsed = sorted((_parse_kline(row) for row in rows), key=lambda item: item.open_time)
    if drop_open_candle and parsed:
        now_ms = int(time.time() * 1000)
        parsed = [item for item in parsed if item.close_time < now_ms]
    if len(parsed) < 60:
        raise MarketError(f"at least 60 closed klines are required; got {len(parsed)}")
    for index, current in enumerate(parsed):
        values = (current.open, current.high, current.low, current.close, current.volume)
        if not all(math.isfinite(value) for value in values):
            raise MarketError("klines contain non-finite values")
        if min(current.open, current.high, current.low, current.close) <= 0 or current.volume < 0:
            raise MarketError("klines contain invalid positive OHLC or volume")
        if current.close_time <= current.open_time:
            raise MarketError("kline close time is invalid")
        if current.high < max(current.open, current.close) or current.low > min(current.open, current.close):
            raise MarketError("kline OHLC values are inconsistent")
        if index:
            previous = parsed[index - 1]
            if current.open_time <= previous.open_time:
                raise MarketError("klines must have strictly increasing timestamps")
            if interval_ms is not None and current.open_time - previous.open_time != interval_ms:
                raise MarketError("klines have an interval gap")
    return parsed


def fetch_klines(settings: Settings, symbol: str, *, validated: bool = False) -> list[Kline]:
    if not validated:
        validate_spot_symbol(settings, symbol)
    query = urllib.parse.urlencode(
        {
            "symbol": symbol,
            "interval": settings.market.interval,
            "limit": PREFILTER_REQUEST_COUNT,
        }
    )
    url = f"{settings.market.base_url}/api/v3/klines?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "RiskPilot-Agent-OS/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.market.request_timeout_seconds) as response:
            if response.status != 200:
                raise MarketError(f"Binance market API returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise MarketError(f"Binance market API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise MarketError(f"Binance market API is unreachable: {exc.reason}") from exc
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise MarketError(f"invalid response from Binance market API: {exc}") from exc
    if not isinstance(payload, list):
        raise MarketError("Binance market API returned an unexpected payload")
    closed = normalize_klines(payload, drop_open_candle=True, interval_ms=_interval_ms(settings.market.interval))
    if len(closed) < ANALYSIS_CANDLE_COUNT:
        raise MarketError("Binance prefilter did not return 60 closed candles")
    return closed[-ANALYSIS_CANDLE_COUNT:]


def load_fixture(path: Path, shift_to_now: bool = False) -> list[Kline]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("klines") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise MarketError("fixture must contain a kline list")
    klines = normalize_klines(rows, drop_open_candle=False)
    if not shift_to_now:
        return klines
    interval_ms = max(60_000, klines[-1].close_time - klines[-1].open_time + 1)
    final_close = int(time.time() * 1000) - 1_000
    shift = final_close - klines[-1].close_time
    return [
        Kline(
            open_time=item.open_time + shift,
            open=item.open,
            high=item.high,
            low=item.low,
            close=item.close,
            volume=item.volume,
            close_time=item.close_time + shift,
        )
        for item in klines
        if interval_ms > 0
    ]


def synthetic_bullish_klines(count: int = 120, final_close_ms: int | None = None) -> list[Kline]:
    """Create deterministic closed candles for tests and a no-money demo."""
    if count < 60:
        raise ValueError("count must be at least 60")
    interval_ms = 15 * 60 * 1000
    ending = final_close_ms or (int(time.time() * 1000) - 2_000)
    starting = ending - count * interval_ms
    result: list[Kline] = []
    prior_close = 100.0
    for index in range(count):
        trend = 0.015 + (0.015 if index >= count - 30 else 0.0)
        wave = math.sin(index / 2.3) * 0.14
        close = prior_close + trend + wave
        opened = prior_close - math.sin(index / 3.0) * 0.03
        high = max(opened, close) + 0.18 + abs(math.sin(index)) * 0.05
        low = min(opened, close) - 0.16 - abs(math.cos(index)) * 0.04
        volume = 100.0 + (index % 8) * 2.5
        if index >= count - 3:
            volume *= 1.7
        open_time = starting + index * interval_ms
        result.append(
            Kline(
                open_time=open_time,
                open=round(opened, 8),
                high=round(high, 8),
                low=round(low, 8),
                close=round(close, 8),
                volume=round(volume, 8),
                close_time=open_time + interval_ms - 1,
            )
        )
        prior_close = close
    return result


def scaled_synthetic_klines(target_close: float, count: int = 120) -> list[Kline]:
    if target_close <= 0:
        raise ValueError("target close must be positive")
    baseline = synthetic_bullish_klines(count=count)
    factor = target_close / baseline[-1].close
    return [
        Kline(
            open_time=item.open_time,
            open=item.open * factor,
            high=item.high * factor,
            low=item.low * factor,
            close=item.close * factor,
            volume=item.volume,
            close_time=item.close_time,
        )
        for item in baseline
    ]


def write_fixture(path: Path, klines: list[Kline]) -> None:
    path.write_text(
        json.dumps({"klines": [item.to_dict() for item in klines]}, indent=2) + "\n",
        encoding="utf-8",
    )



def fetch_1m_candles_since(settings: Settings, symbol: str, start_ms: int,
                           end_ms: int | None = None, max_pages: int = 10) -> list[Kline]:
    if symbol not in settings.market.symbols:
        raise MarketError(f"symbol is not allowlisted: {symbol}")
    ending = end_ms or int(time.time() * 1000)
    cursor = start_ms
    result: list[Kline] = []
    for _ in range(max_pages):
        payload = _public_json(settings, "/api/v3/klines", {
            "symbol": symbol, "interval": "1m", "startTime": str(cursor),
            "endTime": str(ending), "limit": "1000"})
        if not isinstance(payload, list):
            raise MarketError("Binance 1m history payload is invalid")
        page = [_parse_kline(row) for row in payload]
        page = [row for row in page if row.close_time < ending]
        if not page:
            break
        for row in page:
            if result and row.open_time <= result[-1].open_time:
                continue
            result.append(row)
        cursor = result[-1].close_time + 1
        if cursor >= ending or len(page) < 1000:
            break
    if not result or result[0].open_time != start_ms or result[-1].close_time < ending - 120_000:
        raise MarketError("bounded Binance 1m history is incomplete")
    for row in result:
        values = (row.open, row.high, row.low, row.close, row.volume)
        if (not all(math.isfinite(value) for value in values) or min(row.open, row.high, row.low, row.close) <= 0
                or row.volume < 0 or row.high < max(row.open, row.close) or row.low > min(row.open, row.close)):
            raise MarketError("Binance 1m history contains an invalid candle")
    for previous, current in zip(result, result[1:]):
        if current.open_time - previous.open_time != 60_000:
            raise MarketError("Binance 1m history has a gap")
    return result
