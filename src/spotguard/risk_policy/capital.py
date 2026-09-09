from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from ..util import isoformat


def _valid_non_negative(value: Decimal, field: str) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{field} must be finite and non-negative")
    return value


@dataclass(frozen=True)
class AssetValuation:
    asset: str
    symbol: str
    quantity: Decimal
    mark_price: Decimal
    quote_value: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "asset": self.asset,
            "symbol": self.symbol,
            "quantity": format(self.quantity, "f"),
            "mark_price": format(self.mark_price, "f"),
            "quote_value": format(self.quote_value, "f"),
        }


@dataclass(frozen=True)
class EquitySnapshot:
    mode: str
    quote_asset: str
    observed_at: str
    free_quote: Decimal
    locked_quote: Decimal
    spot_assets_value: Decimal
    equity: Decimal
    reserve_quote: Decimal
    available_buying_power: Decimal
    asset_valuations: tuple[AssetValuation, ...]
    valuation_basis: str = "bid_mark"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "quote_asset": self.quote_asset,
            "observed_at": self.observed_at,
            "free_quote": format(self.free_quote, "f"),
            "locked_quote": format(self.locked_quote, "f"),
            "spot_assets_value": format(self.spot_assets_value, "f"),
            "equity": format(self.equity, "f"),
            "reserve_quote": format(self.reserve_quote, "f"),
            "available_buying_power": format(self.available_buying_power, "f"),
            "valuation_basis": self.valuation_basis,
            "asset_valuations": [item.to_dict() for item in self.asset_valuations],
        }


@dataclass(frozen=True)
class UsageSnapshot:
    open_exposure: Decimal
    aggregate_open_risk: Decimal
    daily_realized_loss: Decimal
    economic_positions: int
    active_tranches: int
    weekly_realized_loss: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "open_exposure": format(self.open_exposure, "f"),
            "aggregate_open_risk": format(self.aggregate_open_risk, "f"),
            "daily_realized_loss": format(self.daily_realized_loss, "f"),
            "weekly_realized_loss": format(self.weekly_realized_loss, "f"),
            "economic_positions": self.economic_positions,
            "active_tranches": self.active_tranches,
        }


def build_equity_snapshot(
    *,
    mode: str,
    quote_asset: str,
    free_quote: Decimal,
    locked_quote: Decimal,
    reserve_quote: Decimal,
    asset_valuations: Iterable[AssetValuation] = (),
    observed_at: str | None = None,
) -> EquitySnapshot:
    free = _valid_non_negative(free_quote, "free quote balance")
    locked = _valid_non_negative(locked_quote, "locked quote balance")
    reserve = _valid_non_negative(reserve_quote, "quote reserve")
    valuations = tuple(asset_valuations)
    for item in valuations:
        _valid_non_negative(item.quantity, f"{item.asset} quantity")
        _valid_non_negative(item.mark_price, f"{item.asset} mark price")
        _valid_non_negative(item.quote_value, f"{item.asset} quote value")
    assets_value = sum((item.quote_value for item in valuations), Decimal("0"))
    equity = free + locked + assets_value
    available = max(Decimal("0"), free - reserve)
    return EquitySnapshot(
        mode=mode,
        quote_asset=quote_asset,
        observed_at=observed_at or isoformat(),
        free_quote=free,
        locked_quote=locked,
        spot_assets_value=assets_value,
        equity=equity,
        reserve_quote=reserve,
        available_buying_power=available,
        asset_valuations=valuations,
    )
