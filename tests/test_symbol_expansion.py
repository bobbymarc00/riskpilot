from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from spotguard.config import load_settings
from spotguard.indicators import analyze
from spotguard.market import Kline, SpotMarketSnapshot, SymbolValidationError, scaled_synthetic_klines, synthetic_bullish_klines, validate_spot_symbol
from spotguard.service import SpotGuard
from spotguard.util import utcnow
from tests.helpers import config_dict

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
OWNER = "123456789"


def settings_for(root: Path):
    raw = config_dict(root)
    raw["market"]["symbols"] = SYMBOLS
    raw["live"].pop("allowed_symbols", None)
    raw["paper"]["max_active_tranches"] = 10
    path = root / "config.json"
    path.write_text(json.dumps(raw))
    return load_settings(path)


def snapshot(symbol: str, price: str, step: str) -> SpotMarketSnapshot:
    value = Decimal(price)
    return SpotMarketSnapshot(symbol, value - Decimal("0.001"), value, value,
        Decimal("5"), Decimal(step), "TRADING", 1)


class SymbolExpansionTests(unittest.TestCase):
    def setUp(self):
        self.exchange_info = patch(
            "spotguard.service.validate_spot_symbol",
            return_value={
                "symbol": "BTCUSDT", "status": "TRADING", "quote_asset": "USDT",
                "spot_trading_allowed": True, "market_step_size": "0.001",
                "market_min_qty": "0.001", "min_notional": "5", "price_tick_size": "0.01",
            },
        )
        self.exchange_info.start()

    def tearDown(self):
        self.exchange_info.stop()

    def test_exchange_info_validation_and_market_lot_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(Path(directory))
            payload = {"symbols": [{"symbol": "SOLUSDT", "status": "TRADING",
                "quoteAsset": "USDT", "isSpotTradingAllowed": True,
                "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0", "minQty": "0", "maxQty": "0"},
                    {"filterType": "NOTIONAL", "minNotional": "5"}]}]}
            with patch("spotguard.market._public_json", return_value=payload):
                result = validate_spot_symbol(settings, "SOLUSDT")
            self.assertEqual(result["market_step_size"], "0.001")
            payload["symbols"][0]["isSpotTradingAllowed"] = False
            with patch("spotguard.market._public_json", return_value=payload):
                with self.assertRaises(SymbolValidationError):
                    validate_spot_symbol(settings, "SOLUSDT")

    def test_sol_and_xrp_manual_buy_rounding_balance_and_exit(self):
        cases = (("SOLUSDT", "150", "0.01", Decimal("0.04"), "SOL"),
                 ("XRPUSDT", "0.523", "1", Decimal("11"), "XRP"))
        for symbol, price, step, gross, asset in cases:
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as directory:
                service = SpotGuard(settings_for(Path(directory)))
                snap = snapshot(symbol, price, step)
                with patch("spotguard.service.fetch_klines", return_value=scaled_synthetic_klines(float(price))), \
                     patch("spotguard.service.fetch_spot_snapshot", return_value=snap):
                    proposal = service.create_manual_buy_proposal(symbol, Decimal("6"))["proposal"]
                    token = service.signer.approval_token(proposal["canonical_json"])
                    claim = service.claim(proposal["id"], token, OWNER, OWNER)
                    filled = service.execute_paper(proposal["id"], claim["lease"])
                self.assertEqual(Decimal(filled["execution_summary"]["gross_base_quantity"]), gross)
                position = service.ledger.get_paper_position(filled["execution_summary"]["position_id"])
                balance = service.ledger.paper_balance()
                self.assertIn(asset, balance["assets"])
                target = Decimal(position["final_target"])
                now = int(utcnow().timestamp() * 1000) - 60_000
                candle = Kline(now, float(target), float(target + 1), float(target), float(target), 1, now + 59_999)
                with patch("spotguard.service.fetch_1m_candles_since", return_value=[candle]):
                    service.monitor_paper_positions()
                self.assertNotIn(asset, service.ledger.paper_balance()["assets"])

    def test_ranked_scan_selects_one_fresh_high_score_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(settings_for(Path(directory)))
            base = analyze(synthetic_bullish_klines())
            sol = replace(base, score=85, candle_close_time=base.candle_close_time - 1)
            xrp = replace(base, score=96, candle_close_time=base.candle_close_time)
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch("spotguard.service.analyze", side_effect=[sol, xrp]), \
                 patch.object(service, "_confirm_prefilter_candle", return_value={"matched": True, "failure_reason": None}) as confirm:
                result = service.scan(symbols=["SOLUSDT", "XRPUSDT"])
            confirm.assert_called_once()
            self.assertEqual(confirm.call_args.args[0], "XRPUSDT")
            created = [r["candidate"] for r in result["results"] if r["candidate"]]
            self.assertEqual([c["symbol"] for c in created], ["XRPUSDT"])
            self.assertEqual(result["ranking"][0]["symbol"], "XRPUSDT")

    def test_unavailable_spot_symbol_is_rejected_without_product_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root)
            raw["market"]["symbols"] = SYMBOLS + ["NOTREALUSDT"]
            raw["live"].pop("allowed_symbols", None)
            raw["paper"].update({"max_active_tranches": 10, "max_open_exposure_usdt": 500,
                "max_risk_per_position_usdt": 2, "max_aggregate_risk_usdt": 4})
            path = root / "config.json"; path.write_text(json.dumps(raw))
            service = SpotGuard(load_settings(path))
            calls = []
            def candles(settings, symbol):
                calls.append(("SPOT", symbol))
                if symbol == "NOTREALUSDT":
                    raise SymbolValidationError("NOTREALUSDT unavailable on Binance Spot")
                return synthetic_bullish_klines()
            with patch("spotguard.service.fetch_klines", side_effect=candles), \
                 patch.object(service, "_confirm_prefilter_candle", return_value={"matched": True, "failure_reason": None}):
                result = service.scan(symbols=["SOLUSDT", "NOTREALUSDT"])
            self.assertTrue(any(r["symbol"] == "SOLUSDT" for r in result["results"]))
            self.assertEqual(result["errors"][0]["symbol"], "NOTREALUSDT")
            self.assertIn("Binance Spot", result["errors"][0]["error"])
            self.assertEqual(calls, [("SPOT", "SOLUSDT"), ("SPOT", "NOTREALUSDT")])
            rendered = json.dumps(result).lower()
            self.assertNotIn("futures", rendered)
            self.assertNotIn("convert", rendered)
            with service.ledger.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM events WHERE kind='symbol.validation_rejected' AND entity_id='NOTREALUSDT'").fetchone()[0], 1)

    def test_per_symbol_candle_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(settings_for(Path(directory)))
            first = service.scan(symbols=["SOLUSDT"], synthetic=True)
            second = service.scan(symbols=["SOLUSDT"], synthetic=True)
            self.assertTrue(first["results"][0]["created"])
            self.assertFalse(second["results"][0]["created"])
            self.assertIn("deduplication", second["results"][0]["confirmation"]["failure_reason"])

    def test_full_capacity_skips_agent_but_keeps_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            service = SpotGuard(settings_for(Path(directory)))
            with patch("spotguard.service.fetch_klines", return_value=synthetic_bullish_klines()), \
                 patch.object(service.ledger, "paper_balance", return_value={"open_positions": 5, "active_tranches": 10}), \
                 patch.object(service, "_confirm_prefilter_candle") as confirm:
                result = service.scan(symbols=["SOLUSDT", "XRPUSDT"])
            confirm.assert_not_called()
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(all("active PAPER tranche limit reached" in r["confirmation"]["failure_reason"] for r in result["results"]))

    def test_symbols_status_separates_enabled_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = config_dict(root); raw["market"]["symbols"] = SYMBOLS + ["NOTREALUSDT"]
            raw["live"].pop("allowed_symbols", None)
            raw["paper"].update({"max_active_tranches": 10, "max_open_exposure_usdt": 500,
                "max_risk_per_position_usdt": 2, "max_aggregate_risk_usdt": 4})
            path = root / "config.json"; path.write_text(json.dumps(raw))
            service = SpotGuard(load_settings(path))
            def validation(settings, symbol):
                if symbol == "NOTREALUSDT": raise SymbolValidationError("unavailable on Binance Spot")
                return {"symbol": symbol, "status": "TRADING"}
            with patch("spotguard.service.validate_spot_symbol", side_effect=validation):
                result = service.symbols_status()
            self.assertEqual(len(result["enabled"]), 5)
            self.assertEqual([r["symbol"] for r in result["rejected"]], ["NOTREALUSDT"])


if __name__ == "__main__":
    unittest.main()
